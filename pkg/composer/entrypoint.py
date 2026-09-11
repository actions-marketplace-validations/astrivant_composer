"""
Compile Helm charts into reproducible Docker Compose configurations.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import jsonschema
from yaml import YAMLError

from composer.ast.compose import dump_yaml
from composer.ast.kubernetes import Resource, read_resources
from composer.compiler import Compilation, compile_resources
from composer.exceptions import CompilationError
from composer.profile import mapping, merge
from composer.utils import load_documents

log = logging.getLogger(__name__)
SCHEMAS = Path(__file__).parent / "schemas"
VALUES_MARKER = "composer-internal-coalesced-values"


class JsonFormatter(logging.Formatter):
    """
    Emit one compiler action per JSON log line on stderr.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Format a log row with timestamp and source logger.

        Args:
            record (logging.LogRecord): Standard Python log record.

        Returns:
            str: One JSON object without embedded exception values.
        """
        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "log_level": record.levelname,
                "service": {"name": "composer", "version": "0.1.0"},
                "message": record.getMessage(),
                "context": {"logger": record.name},
            }
        )


class Arguments(argparse.Namespace):
    """
    Hold typed compiler command options.

    Attributes:
        chart (Path | None): Source Helm chart.
        manifest (list[Path] | None): Already-rendered manifest inputs.
        values (list[Path]): Helm values files, in override order.
        profile (Path | None): Declarative runtime profile.
        release (str): Helm release name.
        namespace (str): Helm namespace.
        kube_version (str): Kubernetes capability version used for rendering.
        output (Path): Compose output or drift-check target.
        compare (Path | None): Independent reference Compose file.
        report (Path | None): Optional provenance and comparison report.
        check (bool): Verify drift without modifying outputs.
        log_level (str): Minimum emitted log severity.
    """

    chart: Path | None
    manifest: list[Path] | None
    values: list[Path]
    profile: Path | None
    release: str
    namespace: str
    kube_version: str
    output: Path
    compare: Path | None
    report: Path | None
    check: bool
    log_level: str


def load(path: Path) -> dict[str, object]:
    """
    Parse a YAML object with duplicate-key rejection.

    Args:
        path (Path): YAML input file.

    Returns:
        dict[str, object]: Parsed plain mapping.

    Raises:
        OSError: Input cannot be read.
        CompilationError: Input is not an object.
        YAMLError: YAML input is malformed.
    """
    documents = load_documents(path.read_text())
    if len(documents) != 1:
        raise CompilationError("Expected one YAML document")
    return mapping(documents[0])


def validate(value: dict[str, object], schema_name: str) -> None:
    """
    Validate an input or output against the bundled JSON Schema.

    Args:
        value (dict[str, object]): Profile or Compose document.
        schema_name (str): Bundled schema filename.

    Returns:
        None: The document conforms to the requested schema.

    Raises:
        OSError: A bundled schema cannot be read.
        CompilationError: A field violates the schema.
    """
    schema = json.loads((SCHEMAS / schema_name).read_text())
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = sorted(
        validator.iter_errors(json.loads(json.dumps(value))), key=lambda error: str(error.path)
    )
    if errors:
        paths = ["/" + "/".join(str(part) for part in error.absolute_path) for error in errors]
        raise CompilationError(f"{schema_name} validation failed at {', '.join(paths)}")


def render_chart(args: Arguments, temporary: Path) -> tuple[list[Resource], dict[str, object]]:
    """
    Ask Helm to render resources and its actual coalesced values in a temporary chart.

    Args:
        args (Arguments): Chart and rendering options.
        temporary (Path): Disposable compilation directory.

    Returns:
        tuple[list[Resource], dict[str, object]]: Resources and Helm's coalesced values.

    Raises:
        OSError: Chart files cannot be copied or Helm cannot be executed.
        CompilationError: Rendering fails or coalesced values cannot be obtained.
        subprocess.TimeoutExpired: Helm exceeds the rendering deadline.
    """
    if args.chart is None:
        raise CompilationError("A chart path is required for rendering")
    chart = temporary / "chart"
    shutil.copytree(args.chart.resolve(), chart)
    templates = chart / "templates"
    templates.mkdir(exist_ok=True)
    marker = templates / "composer-internal-values.yaml"
    if marker.exists():
        raise CompilationError("Chart uses reserved template name composer-internal-values.yaml")
    marker.write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
        f"  name: {VALUES_MARKER}\ndata:\n"
        "  values.json: {{ toJson .Values | quote }}\n"
    )
    command = [
        "helm",
        "template",
        args.release,
        str(chart),
        "--namespace",
        args.namespace,
        "--kube-version",
        args.kube_version,
    ]
    for path in args.values:
        command.extend(["--values", str(path.resolve())])
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)
    if result.returncode:
        raise CompilationError(
            f"Helm rendering failed with exit code {result.returncode}; "
            "check chart dependencies and values"
        )
    manifest = temporary / "rendered.yaml"
    manifest.write_text(result.stdout)
    resources = read_resources([manifest])
    markers = [
        resource
        for resource in resources
        if resource.obj.kind == "ConfigMap" and resource.obj.metadata.get("name") == VALUES_MARKER
    ]
    if len(markers) != 1:
        raise CompilationError("Cannot identify Helm's coalesced values")
    values = mapping(json.loads(str(markers[0].obj.data["values.json"])))
    return [resource for resource in resources if resource not in markers], values


def canonical(document: dict[str, object]) -> dict[str, object]:
    """
    Normalize YAML representation differences for runtime contract comparisons.

    Args:
        document (dict[str, object]): Compose document.

    Returns:
        dict[str, object]: Semantically comparable environment, resource, and port values.

    Raises:
        CompilationError: A service mapping is malformed.
    """
    result = merge({}, {key: value for key, value in document.items() if not key.startswith("x-")})
    services = mapping(result.get("services", {}))
    for name, raw in services.items():
        service = mapping(raw)
        if "environment" in service:
            service["environment"] = {
                key: value
                if value is None
                else str(value).lower()
                if isinstance(value, bool)
                else str(value)
                for key, value in mapping(service["environment"]).items()
            }
        for field in ("expose", "ports"):
            value = service.get(field)
            if isinstance(value, list):
                service[field] = [
                    str(item) if not isinstance(item, dict) else item for item in value
                ]
        memory = service.get("mem_limit")
        if isinstance(memory, str):
            match = re.fullmatch(r"([0-9.]+)([kmgt]?)(?:i?b)?", memory, flags=re.IGNORECASE)
            if match:
                factor = 1024 ** ("kmgt".index(match[2].lower()) + 1) if match[2] else 1
                service["mem_limit"] = int(Decimal(match[1]) * factor)
        services[name] = service
    result["services"] = services
    return result


def differences(left: object, right: object, path: str = "") -> list[str]:
    """
    Report differing paths without logging credentials or configuration values.

    Args:
        left (object): Generated runtime contract.
        right (object): Independent reference contract.
        path (str): Current JSON pointer.

    Returns:
        list[str]: Differing paths, ignoring object order.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        before, after = mapping(left), mapping(right)
        result = [path + "/" + key for key in before.keys() ^ after.keys()]
        for key in before.keys() & after.keys():
            escaped = key.replace("~", "~0").replace("/", "~1")
            result.extend(differences(before[key], after[key], path + "/" + escaped))
        return sorted(result)
    return [path or "/"] if left != right else []


def write_outputs(compilation: Compilation, output: Path) -> None:
    """
    Write validated Compose output and only referenced generated mount files.

    Args:
        compilation (Compilation): Validated runtime output.
        output (Path): Compose file location; bind mounts are relative to its parent.

    Returns:
        None: Output and required support files have been written.

    Raises:
        OSError: Output files cannot be written.
        CompilationError: A generated file escapes the support directory.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    root = (output.parent / ".compose-generated").resolve()
    for path, content in compilation.files.items():
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise CompilationError("Generated mount escapes the support directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o600)
    output.write_text(
        "# Generated by Composer from Helm and a runtime profile; regenerate instead of editing.\n"
        + dump_yaml(compilation.document)
    )


def main() -> int:
    """
    Render, compile, validate, compare, and optionally write a Compose configuration.

    Returns:
        int: Zero for success, one for drift, or two for an invalid compilation.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--chart", type=Path, help="Helm chart with built dependencies")
    inputs.add_argument(
        "--manifest", type=Path, action="append", help="Rendered manifest; repeat to combine inputs"
    )
    parser.add_argument(
        "--values",
        "-f",
        type=Path,
        action="append",
        default=[],
        help="Helm values override; repeat in precedence order",
    )
    parser.add_argument("--profile", type=Path, help="Declarative runtime profile")
    parser.add_argument("--release", default="composer", help="Helm release name")
    parser.add_argument("--namespace", default="default", help="Helm release namespace")
    parser.add_argument(
        "--kube-version", default="1.37.0", help="Kubernetes capabilities for rendering"
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("compose.yaml"),
        help="Compose output or drift-check target",
    )
    parser.add_argument(
        "--compare", type=Path, help="Compare against an independent Compose reference"
    )
    parser.add_argument(
        "--report", type=Path, help="Write provenance and comparison results as JSON"
    )
    parser.add_argument(
        "--check", action="store_true", help="Check output drift without rewriting files"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="WARNING",
        help="Compiler log severity",
    )
    args = parser.parse_args(namespace=Arguments())
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=args.log_level, handlers=[handler], force=True)
    try:
        profile = load(args.profile) if args.profile else {}
        if profile:
            validate(profile, "profile.schema.json")
        with TemporaryDirectory(prefix="composer-") as directory:
            if args.chart:
                resources, values = render_chart(args, Path(directory))
            else:
                resources = read_resources(args.manifest or [])
                values = {}
                for path in args.values:
                    values = merge(values, load(path))
            compilation = compile_resources(resources, profile, values)
        validate(compilation.document, "compose-spec.json")
        reference = args.compare or (args.output if args.check else None)
        changed = (
            differences(canonical(compilation.document), canonical(load(reference)))
            if reference
            else []
        )
        if not args.check:
            write_outputs(compilation, args.output)
        report = {
            "services": compilation.provenance,
            "service_count": len(mapping(compilation.document["services"])),
            "volume_count": len(mapping(compilation.document.get("volumes", {}))),
            "comparison": {"matches": not changed, "differences": changed},
        }
        if args.report and not args.check:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "services": report["service_count"],
                    "matching_reference": not changed,
                    "differences": changed,
                }
            )
        )
        return 1 if changed else 0
    except YAMLError:
        log.error("Malformed YAML input; inspect the input document syntax")
        return 2
    except (CompilationError, OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
        log.error("Compilation failed: %s", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
