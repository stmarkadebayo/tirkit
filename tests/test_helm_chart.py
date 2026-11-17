"""Static checks on the Helm chart that run without helm. CI also runs ``helm lint`` and
``kubeconform`` on the rendered manifests (.github/workflows/ops.yml)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "tirkit"


def test_chart_and_values_parse() -> None:
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["apiVersion"] == "v2" and chart["name"] == "tirkit"
    for path in [CHART / "values.yaml", *sorted((CHART.parent / "ci").glob("*.yaml"))]:
        assert isinstance(yaml.safe_load(path.read_text()), dict), path


def test_pods_run_as_non_root_with_a_read_only_root() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    pod, container = values["podSecurityContext"], values["securityContext"]
    assert pod["runAsNonRoot"] is True and pod["runAsUser"] != 0
    assert container["allowPrivilegeEscalation"] is False
    assert container["readOnlyRootFilesystem"] is True
    assert container["capabilities"]["drop"] == ["ALL"]
    for name in ("server-deployment.yaml", "worker-deployment.yaml", "postgresql.yaml"):
        text = (CHART / "templates" / name).read_text()
        assert "securityContext:" in text and "automountServiceAccountToken: false" in text, name


def test_server_probes_use_the_health_endpoint() -> None:
    text = (CHART / "templates" / "server-deployment.yaml").read_text()
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        block = text.split(f"{probe}:", 1)[1].split("Probe:", 1)[0]
        assert "path: /api/health" in block, probe


def test_every_values_path_used_by_templates_exists() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    used = set()
    for tpl in (CHART / "templates").iterdir():
        used |= set(re.findall(r"\.Values\.([A-Za-z0-9_.]+)", tpl.read_text()))
    for path in sorted(used):
        node = values
        for part in path.split("."):
            assert isinstance(node, dict) and part in node, f".Values.{path} is not in values.yaml"
            node = node[part]


def test_secrets_never_have_defaults_in_values() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    secret_like = [
        values["secrets"][k]
        for k in ("databaseUrl", "adminPassword", "llmApiKey", "anthropicApiKey")
    ]
    assert secret_like == ["", "", "", ""]
    assert values["postgresql"]["auth"]["password"] == ""
    assert values["metrics"]["token"] == ""
