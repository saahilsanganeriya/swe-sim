import modal
app = modal.App("swesim-stakeholder-probe")
image = modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]")

@app.function(image=image)
@modal.fastapi_endpoint(method="GET")
def ping():
    return {"ok": True, "service": "swesim-stakeholder-probe"}
