"""
extractor.py
============
Handles TWO Textract response formats:

Path A — AnalyzeExpense (scanned / image-based documents):
  parse_expense_document()       — maps ExpenseDocument fields → DynamoDB dict
  get_overall_confidence()       — weighted confidence score
  is_valid_invoice()             — Gate 1 semantic check

Path B — AnalyzeDocument (digitally created PDFs):
  parse_document_blocks()        — maps KEY_VALUE_SET + TABLE Blocks → DynamoDB dict
  get_blocks_confidence()        — average confidence across all blocks
  is_valid_invoice_from_blocks() — Gate 1 semantic check for block format
"""

import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any


# ═════════════════════════════════════════════════════════════════════════════
# SHARED CONFIG
# ═════════════════════════════════════════════════════════════════════════════

# ── Path A: AnalyzeExpense field mappings ─────────────────────────────────────
SUMMARY_FIELD_MAP: dict[str, str] = {
    "VENDOR_NAME":            "vendorName",
    "VENDOR_ADDRESS":         "vendorAddress",
    "VENDOR_PHONE":           "vendorPhone",
    "INVOICE_RECEIPT_ID":     "invoiceNumber",
    "INVOICE_RECEIPT_DATE":   "invoiceDate",
    "DUE_DATE":               "dueDate",
    "SUBTOTAL":               "subtotal",
    "TAX":                    "tax",
    "TOTAL":                  "totalAmount",
    "AMOUNT_PAID":            "totalAmount",
    "PAYMENT_TERMS":          "paymentTerms",
    "PO_NUMBER":              "poNumber",
    "RECEIVER_NAME":          "receiverName",
    "RECEIVER_ADDRESS":       "receiverAddress",
}

LINE_ITEM_FIELD_MAP: dict[str, str] = {
    "ITEM":         "description",
    "PRODUCT_CODE": "productCode",
    "QUANTITY":     "quantity",
    "UNIT_PRICE":   "unitPrice",
    "PRICE":        "amount",
    "EXPENSE_ROW":  "rawRow",
}

# ── Path B: AnalyzeDocument keyword → DynamoDB field mappings ─────────────────
# Keys are lowercase for case-insensitive matching against detected form labels
DOCUMENT_FIELD_MAP: dict[str, str] = {
    # Vendor
    "vendor":               "vendorName",
    "vendor name":          "vendorName",
    "supplier":             "vendorName",
    "supplier name":        "vendorName",
    "company":              "vendorName",
    "billed by":            "vendorName",
    "from":                 "vendorName",
    "seller":               "vendorName",

    # Invoice metadata
    "invoice #":            "invoiceNumber",
    "invoice no":           "invoiceNumber",
    "invoice number":       "invoiceNumber",
    "receipt #":            "invoiceNumber",
    "receipt no":           "invoiceNumber",
    "invoice date":         "invoiceDate",
    "date":                 "invoiceDate",
    "issue date":           "invoiceDate",
    "due date":             "dueDate",
    "payment due":          "dueDate",
    "payment due date":     "dueDate",
    "po number":            "poNumber",
    "purchase order":       "poNumber",
    "payment terms":        "paymentTerms",
    "terms":                "paymentTerms",

    # Financial
    "subtotal":             "subtotal",
    "sub total":            "subtotal",
    "sub-total":            "subtotal",
    "tax":                  "tax",
    "vat":                  "tax",
    "gst":                  "tax",
    "hst":                  "tax",
    "total":                "totalAmount",
    "total amount":         "totalAmount",
    "total due":            "totalAmount",
    "amount due":           "totalAmount",
    "balance due":          "totalAmount",
    "grand total":          "totalAmount",
    "amount paid":          "totalAmount",

    # Receiver
    "bill to":              "receiverName",
    "billed to":            "receiverName",
    "client":               "receiverName",
    "customer":             "receiverName",
    "to":                   "receiverName",
}

# ── Gate 1 config (shared) ────────────────────────────────────────────────────
# Required Textract AnalyzeExpense field types for invoice validation
INVOICE_REQUIRED_FIELDS: set[str] = {
    "VENDOR_NAME", "TOTAL", "SUBTOTAL",
    "INVOICE_RECEIPT_ID", "INVOICE_RECEIPT_DATE", "AMOUNT_PAID",
}

# Required DynamoDB keys for AnalyzeDocument invoice validation
INVOICE_REQUIRED_DB_KEYS: set[str] = {
    "vendorName", "totalAmount", "invoiceNumber", "invoiceDate", "subtotal",
}

MIN_REQUIRED_FIELDS  = 2
MIN_FIELD_CONFIDENCE = 70.0

# ── Confidence weighting (Path A) ────────────────────────────────────────────
HIGH_WEIGHT_FIELDS: set[str] = {
    "VENDOR_NAME", "TOTAL", "AMOUNT_PAID", "INVOICE_RECEIPT_ID",
}


# ═════════════════════════════════════════════════════════════════════════════
# PATH A — AnalyzeExpense
# ═════════════════════════════════════════════════════════════════════════════

def is_valid_invoice(expense_doc: dict) -> tuple[bool, str]:
    """
    Gate 1 for Path A.
    Validates the ExpenseDocument contains enough invoice-specific fields
    at sufficient confidence to qualify as an invoice or receipt.
    """
    summary_fields = expense_doc.get("SummaryFields", [])

    if not summary_fields:
        return False, (
            "Textract returned no summary fields — "
            "document does not appear to be an invoice or receipt"
        )

    matched  = []
    low_conf = []

    for field in summary_fields:
        field_type  = _get_field_type(field)
        field_value = _get_field_value(field)
        confidence  = _get_confidence(field)

        if field_type not in INVOICE_REQUIRED_FIELDS or not field_value:
            continue

        if confidence >= MIN_FIELD_CONFIDENCE:
            matched.append(field_type)
        else:
            low_conf.append(f"{field_type}({confidence:.0f}%)")

    if len(matched) >= MIN_REQUIRED_FIELDS:
        return True, ""

    return False, (
        f"Only {len(matched)} of {MIN_REQUIRED_FIELDS} required invoice fields "
        f"detected above {MIN_FIELD_CONFIDENCE}% confidence. "
        f"Matched: {matched or 'none'}. Low-confidence: {low_conf or 'none'}."
    )


def get_overall_confidence(expense_doc: dict) -> float:
    """
    Weighted confidence score for Path A (AnalyzeExpense).
    High-value fields (VENDOR_NAME, TOTAL etc.) are weighted 2×.
    Empty fields are excluded. Returns 0.0 if nothing detected.
    """
    total_weight = 0.0
    weighted_sum = 0.0

    for field in expense_doc.get("SummaryFields", []):
        field_type  = _get_field_type(field)
        field_value = _get_field_value(field)
        confidence  = _get_confidence(field)

        if not field_value:
            continue

        weight        = 2.0 if field_type in HIGH_WEIGHT_FIELDS else 1.0
        weighted_sum += confidence * weight
        total_weight += weight

    return round(weighted_sum / total_weight, 2) if total_weight > 0 else 0.0


def parse_expense_document(
    expense_doc: dict,
    bucket:      str,
    key:         str,
    doc_index:   int,
) -> dict[str, Any]:
    """
    Converts a Textract ExpenseDocument into a flat DynamoDB-ready dict.
    Higher-confidence value wins when two fields map to the same DB key.
    """
    now = datetime.now(timezone.utc)

    invoice: dict[str, Any] = {
        "invoiceId":       _generate_id(bucket, key, doc_index),
        "timestamp":       now.isoformat(),
        "s3Bucket":        bucket,
        "s3Key":           key,
        "confidenceScore": str(get_overall_confidence(expense_doc)),
        "ttl":             int((now + timedelta(days=365)).timestamp()),
    }

    confidence_tracker: dict[str, float] = {}

    for field in expense_doc.get("SummaryFields", []):
        field_type  = _get_field_type(field)
        field_value = _get_field_value(field)
        confidence  = _get_confidence(field)

        if not field_type or not field_value:
            continue

        db_key = SUMMARY_FIELD_MAP.get(field_type)
        if not db_key:
            continue

        if confidence >= confidence_tracker.get(db_key, -1.0):
            invoice[db_key] = field_value
            confidence_tracker[db_key] = confidence

    line_items = _parse_line_items(expense_doc.get("LineItemGroups", []))
    if line_items:
        invoice["lineItems"]     = line_items
        invoice["lineItemCount"] = len(line_items)

    return invoice


# ── Private helpers (Path A) ──────────────────────────────────────────────────
def _parse_line_items(groups: list) -> list[dict]:
    items = []
    for group in groups:
        for line_item in group.get("LineItems", []):
            item = {}
            for field in line_item.get("LineItemExpenseFields", []):
                ft = _get_field_type(field)
                fv = _get_field_value(field)
                if ft and fv:
                    db_key = LINE_ITEM_FIELD_MAP.get(ft)
                    if db_key:
                        item[db_key] = fv
            if item:
                items.append(item)
    return items


def _get_field_type(field: dict) -> str:
    return (field.get("Type") or {}).get("Text", "").strip().upper()


def _get_field_value(field: dict) -> str:
    return ((field.get("ValueDetection") or {}).get("Text") or "").strip()


def _get_confidence(field: dict) -> float:
    try:
        return float((field.get("ValueDetection") or {}).get("Confidence", 0))
    except (TypeError, ValueError):
        return 0.0


# ═════════════════════════════════════════════════════════════════════════════
# PATH B — AnalyzeDocument
# ═════════════════════════════════════════════════════════════════════════════

def is_valid_invoice_from_blocks(blocks: list) -> tuple[bool, str]:
    """
    Gate 1 for Path B.
    Validates the AnalyzeDocument Blocks contain enough invoice-like
    key-value pairs to qualify as an invoice.

    AnalyzeDocument returns KEY_VALUE_SET blocks where:
      KEY block   → the label  (e.g. "Invoice #")
      VALUE block → the value  (e.g. "INV-001")
    """
    if not blocks:
        return False, "AnalyzeDocument returned no blocks"

    kv_pairs = _extract_key_value_pairs(blocks)

    if not kv_pairs:
        return False, "No key-value pairs found — document may not be form-based"

    matched = []
    for raw_key, value in kv_pairs.items():
        if not value:
            continue
        db_key = DOCUMENT_FIELD_MAP.get(raw_key.lower().strip())
        if db_key and db_key in INVOICE_REQUIRED_DB_KEYS:
            matched.append(db_key)

    # Deduplicate
    matched = list(set(matched))

    if len(matched) >= MIN_REQUIRED_FIELDS:
        return True, ""

    return False, (
        f"Only {len(matched)} of {MIN_REQUIRED_FIELDS} required invoice fields "
        f"identified in document key-value pairs. "
        f"Matched DB keys: {matched or 'none'}."
    )


def get_blocks_confidence(blocks: list) -> float:
    """
    Average confidence score for Path B (AnalyzeDocument).
    Computed across all WORD and KEY_VALUE_SET blocks with non-empty text.
    Returns 0.0 if no scoreable blocks found.
    """
    scores = []
    for block in blocks:
        if block.get("BlockType") not in ("WORD", "KEY_VALUE_SET"):
            continue
        conf = block.get("Confidence")
        text = block.get("Text", "").strip()
        if conf is not None and text:
            scores.append(float(conf))

    return round(sum(scores) / len(scores), 2) if scores else 0.0


def parse_document_blocks(
    blocks: list,
    bucket: str,
    key:    str,
) -> dict[str, Any]:
    """
    Converts Textract AnalyzeDocument Blocks into a flat DynamoDB-ready dict.

    Processes:
      - KEY_VALUE_SET blocks → form field key-value pairs (vendor, total, date etc.)
      - TABLE blocks         → line items

    Note: AnalyzeDocument does not return structured expense fields like
    AnalyzeExpense does. We map form labels (e.g. "Invoice #", "Total Due")
    to our DynamoDB schema using DOCUMENT_FIELD_MAP.
    """
    now = datetime.now(timezone.utc)

    invoice: dict[str, Any] = {
        "invoiceId":       _generate_id(bucket, key, 0),
        "timestamp":       now.isoformat(),
        "s3Bucket":        bucket,
        "s3Key":           key,
        "confidenceScore": str(get_blocks_confidence(blocks)),
        "ttl":             int((now + timedelta(days=365)).timestamp()),
    }

    # ── Extract key-value pairs (form fields) ─────────────────────────────────
    kv_pairs = _extract_key_value_pairs(blocks)

    for raw_key, value in kv_pairs.items():
        if not value:
            continue
        db_key = DOCUMENT_FIELD_MAP.get(raw_key.lower().strip())
        if db_key and db_key not in invoice:
            invoice[db_key] = value

    # ── Extract line items from tables ────────────────────────────────────────
    line_items = _extract_table_line_items(blocks)
    if line_items:
        invoice["lineItems"]     = line_items
        invoice["lineItemCount"] = len(line_items)

    return invoice


# ── Private helpers (Path B) ──────────────────────────────────────────────────
def _extract_key_value_pairs(blocks: list) -> dict[str, str]:
    """
    Builds a key→value dict from KEY_VALUE_SET blocks.

    Textract represents form fields as a graph of linked blocks:
      KEY block has EntityTypes=["KEY"] and Relationships pointing to VALUE block
      VALUE block has EntityTypes=["VALUE"] and Relationships pointing to WORD blocks
      WORD blocks contain the actual text

    We build an id→block index, then walk the graph to assemble key-value pairs.
    """
    block_map: dict[str, dict] = {b["Id"]: b for b in blocks}
    kv_pairs:  dict[str, str]  = {}

    for block in blocks:
        if block.get("BlockType") != "KEY_VALUE_SET":
            continue
        if "KEY" not in block.get("EntityTypes", []):
            continue

        key_text   = _get_text_from_block(block, block_map)
        value_text = ""

        # Find the VALUE block linked from this KEY block
        for rel in block.get("Relationships", []):
            if rel["Type"] == "VALUE":
                for val_id in rel["Ids"]:
                    val_block  = block_map.get(val_id, {})
                    value_text = _get_text_from_block(val_block, block_map)
                    break

        if key_text:
            kv_pairs[key_text.strip()] = value_text.strip()

    return kv_pairs


def _get_text_from_block(block: dict, block_map: dict[str, dict]) -> str:
    """
    Assembles text from a block by following its CHILD relationships to WORD blocks.
    """
    words = []
    for rel in block.get("Relationships", []):
        if rel["Type"] == "CHILD":
            for child_id in rel["Ids"]:
                child = block_map.get(child_id, {})
                if child.get("BlockType") == "WORD":
                    words.append(child.get("Text", ""))
    return " ".join(words)


def _extract_table_line_items(blocks: list) -> list[dict]:
    """
    Extracts rows from TABLE blocks as line items.

    Textract TABLE structure:
      TABLE block → CELL blocks (via CHILD relationship)
      CELL block has RowIndex and ColumnIndex
      CELL block → WORD blocks (via CHILD relationship)

    We treat the first row as headers and subsequent rows as data rows.
    Skips tables that don't look like line item tables (< 2 columns or rows).
    """
    block_map: dict[str, dict] = {b["Id"]: b for b in blocks}
    line_items: list[dict]     = []

    for block in blocks:
        if block.get("BlockType") != "TABLE":
            continue

        # Build row→col→text grid
        grid: dict[int, dict[int, str]] = {}
        for rel in block.get("Relationships", []):
            if rel["Type"] != "CHILD":
                continue
            for cell_id in rel["Ids"]:
                cell = block_map.get(cell_id, {})
                if cell.get("BlockType") != "CELL":
                    continue
                row = cell.get("RowIndex", 0)
                col = cell.get("ColumnIndex", 0)
                text = _get_text_from_block(cell, block_map)
                grid.setdefault(row, {})[col] = text

        if len(grid) < 2:
            continue  # need at least a header row + one data row

        # First row = headers
        header_row  = grid.get(min(grid.keys()), {})
        headers     = {col: txt.lower().strip() for col, txt in header_row.items()}

        # Remaining rows = data
        for row_idx in sorted(grid.keys())[1:]:
            row_data = grid[row_idx]
            item     = {}
            for col, header_text in headers.items():
                cell_text = row_data.get(col, "").strip()
                if not cell_text:
                    continue
                # Map header labels to line item fields
                if any(k in header_text for k in ("item", "description", "product", "service")):
                    item["description"] = cell_text
                elif any(k in header_text for k in ("qty", "quantity")):
                    item["quantity"] = cell_text
                elif any(k in header_text for k in ("unit price", "rate", "price each")):
                    item["unitPrice"] = cell_text
                elif any(k in header_text for k in ("amount", "total", "price")):
                    item["amount"] = cell_text
                elif "code" in header_text:
                    item["productCode"] = cell_text

            if item:
                line_items.append(item)

    return line_items


# ═════════════════════════════════════════════════════════════════════════════
# SHARED UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def _generate_id(bucket: str, key: str, doc_index: int) -> str:
    """Deterministic unique invoice ID: <8-char MD5>-<unix-ms>"""
    raw        = f"{bucket}/{key}#{doc_index}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
    ts         = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"{short_hash}-{ts}"
