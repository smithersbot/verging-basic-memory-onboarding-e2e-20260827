"""Railway configuration for the Verging Memory CI test deployment.

This declares the single non-production service that serves
``integrations/verging-memory-ci/adapter.py``. It replaces the deprecated
``railway.json`` config-as-code file, which this Railway version no longer
applies (the container fell back to the image's MCP-server CMD).

Nothing here touches production: the project holds exactly one service, used
only as the Verging Memory CI test endpoint.
"""

from railway_sdk import define_railway, github, preserve, project, service

# One service, authored in its own file. Combine into a project-wide file if
# this deployment ever grows a second resource.
PARTIAL = "memory-ci-deploy-240deb0"

NAMESPACE_ROOT = "/home/appuser/verging/namespaces"


@define_railway
def main(ctx=None):
    memory_ci_deploy = service(
        "memory-ci-deploy-240deb0",
        source=github(
            "smithersbot/verging-basic-memory-onboarding-e2e-20260827",
            branch="verging-wiring-check-20260829-bf337f",
        ),
        # The repository Dockerfile builds the product; the adapter is started
        # instead of the image's MCP-server CMD.
        start="python integrations/verging-memory-ci/serve.py",
        healthcheck="/v1/health",
        healthcheckTimeout=300,
        variables={
            # Notes live under the container user's home, never inside the
            # source checkout at /app, so nothing a report commit could ever
            # capture sits next to test data.
            "VERGING_ADAPTER_DATA_DIR": NAMESPACE_ROOT,
            "BASIC_MEMORY_PROJECT_ROOT": NAMESPACE_ROOT,
            "BASIC_MEMORY_HOME": f"{NAMESPACE_ROOT}/main",
            "BASIC_MEMORY_CONFIG_DIR": "/home/appuser/verging/config",
            # Full-text search is Basic Memory's default retrieval mode and
            # needs no model download, which keeps the test endpoint fast and
            # independent of an embedding provider.
            "BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED": "false",
            "LOGFIRE_IGNORE_NO_CONFIG": "1",
            # The scoped product credential is set directly on the service and
            # deliberately has no value here: it must never enter the
            # repository. `preserve()` keeps whatever Railway already holds.
            "VERGING_PRODUCT_KEY": preserve(),
        },
    )
    return project("memory-ci-deploy-240deb0", resources=[memory_ci_deploy])
