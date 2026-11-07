"""
REST API over the same pipeline the Streamlit app uses.

    pip install -r requirements-api.txt
    uvicorn api:app --reload          # interactive docs at http://localhost:8000/docs

    curl -F "file=@sample_invoices/01_happy_path_nimbus.pdf" http://localhost:8000/v1/invoices

It is the "integration" face of the system: another program (an email
listener, an ERP, a workflow tool) POSTs a PDF and gets the decision, the
reasons and, if approved, the accounting hand-off back as JSON. Nothing here
re-implements logic; every route calls the same core modules, so the API and
the UI can never disagree.

Settings (environment variables):
  API_KEY            if set, every /v1 call must send it as the X-API-Key header
  ANTHROPIC_API_KEY  enables the guarded AI step (see core/ai_assist.py)
  ALERT_WEBHOOK_URL  enables reviewer alerts (see core/notify.py)

Each client (the X-Client-Id header, default "default") gets its own private
audit trail and vendor/PO data, exactly like a browser session in the app.
"""
import dataclasses
import hmac
import json
import os
import uuid

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from core import database, export, matching
from core.pipeline import run_pipeline
from core.workspace import Workspace, workspace_for_client

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

app = FastAPI(
    title="Invoice processing API",
    version="1.0.0",
    description="Send a vendor invoice PDF, get back an explainable Approved / Needs Review / Rejected decision.",
)


def check_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("API_KEY")
    if expected and not (x_api_key and hmac.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key header.")


def client_workspace(x_client_id: str = Header(default="default", max_length=80)) -> Workspace:
    ws = workspace_for_client(x_client_id)
    database.init_db(ws)
    return ws


def _serialise(result) -> dict:
    body = dataclasses.asdict(result)
    body.pop("file_hash", None)
    return body


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/invoices", dependencies=[Depends(check_key)])
async def process_invoice(file: UploadFile = File(..., description="The invoice as a PDF"),
                          use_ai: bool = Query(True, description="Allow the guarded AI step on hard documents"),
                          ws: Workspace = Depends(client_workspace)) -> dict:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is larger than 10 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=415, detail="Only PDF files are accepted.")
    path = ws.uploads_dir / f"{uuid.uuid4().hex}.pdf"       # never use the client's file name as a path
    path.write_bytes(data)
    name = os.path.basename(file.filename or "invoice.pdf")[:120]
    try:
        result = None
        for item in run_pipeline(str(path), name, ws=ws, use_ai=use_ai):
            if hasattr(item, "decision"):
                result = item
    except Exception as err:                                  # an unreadable PDF is a client problem, not a crash
        raise HTTPException(status_code=422, detail=f"Could not process this PDF ({type(err).__name__}).")
    finally:
        path.unlink(missing_ok=True)
    body = _serialise(result)
    body["bill"] = export.erp_bill(database.get_run(result.invoice_id, ws))
    return body


@app.get("/v1/invoices", dependencies=[Depends(check_key)])
def list_invoices(ws: Workspace = Depends(client_workspace)) -> list:
    keep = ("invoice_id", "filename", "processed_at", "vendor_name", "invoice_number", "po_number", "total",
            "decision_status", "human_decision", "effective_status", "decision_reasons")
    return [{k: row.get(k) for k in keep} for row in database.get_all_history(ws)]


@app.get("/v1/invoices/{invoice_id}", dependencies=[Depends(check_key)])
def get_invoice(invoice_id: str, ws: Workspace = Depends(client_workspace)) -> dict:
    row = database.get_run(invoice_id, ws)
    if row is None:
        raise HTTPException(status_code=404, detail="No such invoice.")
    bill = export.erp_bill(row)
    row = dict(row)
    row["full_result"] = json.loads(row.pop("full_result_json") or "{}")
    row["bill"] = bill
    return row


class ReviewIn(BaseModel):
    decision: str = Field(pattern="^(Approved|Rejected)$")
    note: str = Field(default="", max_length=500)


@app.post("/v1/invoices/{invoice_id}/review", dependencies=[Depends(check_key)])
def review_invoice(invoice_id: str, body: ReviewIn, ws: Workspace = Depends(client_workspace)) -> dict:
    """A reviewer resolves an invoice the system marked Needs Review."""
    try:
        database.record_review(invoice_id, body.decision, body.note, ws)
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err))
    return get_invoice(invoice_id, ws)


class VendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    status: str = Field(default="active", pattern="^(active|inactive)$")


class PurchaseOrderIn(BaseModel):
    po_number: str = Field(min_length=1, max_length=40)
    vendor_id: str = Field(min_length=1, max_length=20)
    amount: float = Field(gt=0, lt=1e10)


@app.post("/v1/vendors", status_code=201, dependencies=[Depends(check_key)])
def add_vendor(body: VendorIn, ws: Workspace = Depends(client_workspace)) -> dict:
    try:
        return {"vendor_id": matching.add_vendor(body.name, body.status, ws=ws)}
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err))


@app.post("/v1/purchase-orders", status_code=201, dependencies=[Depends(check_key)])
def add_purchase_order(body: PurchaseOrderIn, ws: Workspace = Depends(client_workspace)) -> dict:
    try:
        return {"po_number": matching.add_purchase_order(body.po_number, body.vendor_id, body.amount, ws=ws)}
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err))
