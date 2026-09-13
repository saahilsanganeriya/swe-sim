"""Modal deployment of the stakeholder service (deploy with ``scripts/deploy_stakeholder.sh``).

Secrets (provider keys and the worker secret) come from the Modal Secret ``swesim-llm`` at runtime; nothing is
baked into the image. Session snapshots live on the Volume ``swesim-stakeholder-state`` under ``/state/sessions``;
the service commits after every write and reloads before every transaction. One container at a time keeps the
in-process lock authoritative.
"""
import modal

app = modal.App("swesim-stakeholder")
volume = modal.Volume.from_name("swesim-stakeholder-state", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi[standard]", "pydantic>=2.7", "anthropic>=0.40", "openai>=1.50", "python-dotenv>=1.0")
    .env({"SWESIM_SESSION_STORE": "/state/sessions"})
    .add_local_python_source("swesim")
)


@app.function(image=image, secrets=[modal.Secret.from_name("swesim-llm")], volumes={"/state": volume},
              min_containers=1, max_containers=1, timeout=600)
@modal.asgi_app()
def web():
    from swesim.stakeholder import service
    service.SYNC = volume.commit.aio
    service.RELOAD = volume.reload.aio
    return service.app
