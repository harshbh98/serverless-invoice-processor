"""
test_extractor.py
=================
Unit tests for both extraction paths:
  Path A — AnalyzeExpense  (is_valid_invoice, get_overall_confidence, parse_expense_document)
  Path B — AnalyzeDocument (is_valid_invoice_from_blocks, get_blocks_confidence, parse_document_blocks)

Run with: python -m pytest test_extractor.py -v
"""

import pytest
from extractor import (
    # Path A
    is_valid_invoice,
    get_overall_confidence,
    parse_expense_document,
    # Path B
    is_valid_invoice_from_blocks,
    get_blocks_confidence,
    parse_document_blocks,
    # Shared helpers
    _get_field_type,
    _get_field_value,
    _get_confidence,
    _extract_key_value_pairs,
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def make_field(field_type: str, value: str, confidence: float = 98.0) -> dict:
    return {
        "Type":           {"Text": field_type, "Confidence": 99.0},
        "ValueDetection": {"Text": value,      "Confidence": confidence},
    }

def make_line_item(*fields: tuple[str, str]) -> dict:
    return {"LineItemExpenseFields": [make_field(ft, fv) for ft, fv in fields]}

def make_expense_doc(summary_fields=None, line_item_groups=None) -> dict:
    return {
        "SummaryFields":  summary_fields  or [],
        "LineItemGroups": line_item_groups or [],
    }

def make_kv_blocks(pairs: dict[str, str], start_id: int = 0) -> list[dict]:
    """
    Builds minimal KEY_VALUE_SET + WORD blocks for testing Path B parsing.
    Each pair becomes: KEY block → VALUE block → WORD block(s).
    """
    blocks = []
    id_counter = [start_id]

    def next_id() -> str:
        id_counter[0] += 1
        return f"block-{id_counter[0]:04d}"

    for key_text, val_text in pairs.items():
        key_word_id  = next_id()
        val_word_id  = next_id()
        value_blk_id = next_id()
        key_blk_id   = next_id()

        blocks.append({"Id": key_word_id,  "BlockType": "WORD", "Text": key_text,  "Confidence": 98.0, "Relationships": []})
        blocks.append({"Id": val_word_id,  "BlockType": "WORD", "Text": val_text,  "Confidence": 95.0, "Relationships": []})
        blocks.append({
            "Id": value_blk_id, "BlockType": "KEY_VALUE_SET",
            "EntityTypes": ["VALUE"],
            "Confidence": 95.0,
            "Relationships": [{"Type": "CHILD", "Ids": [val_word_id]}],
        })
        blocks.append({
            "Id": key_blk_id, "BlockType": "KEY_VALUE_SET",
            "EntityTypes": ["KEY"],
            "Confidence": 98.0,
            "Relationships": [
                {"Type": "CHILD",  "Ids": [key_word_id]},
                {"Type": "VALUE",  "Ids": [value_blk_id]},
            ],
        })
    return blocks


# ═════════════════════════════════════════════════════════════════════════════
# PATH A — AnalyzeExpense Tests
# ═════════════════════════════════════════════════════════════════════════════

class TestIsValidInvoice:

    def test_valid_invoice_passes(self):
        doc = make_expense_doc([
            make_field("VENDOR_NAME",          "Acme Corp",  95.0),
            make_field("TOTAL",                "$1,000.00",  97.0),
            make_field("INVOICE_RECEIPT_DATE", "2024-01-15", 92.0),
        ])
        valid, reason = is_valid_invoice(doc)
        assert valid is True
        assert reason == ""

    def test_empty_doc_fails(self):
        valid, reason = is_valid_invoice(make_expense_doc())
        assert valid is False
        assert "no summary fields" in reason.lower()

    def test_only_one_required_field_fails(self):
        doc = make_expense_doc([make_field("VENDOR_NAME", "Acme", 95.0)])
        valid, reason = is_valid_invoice(doc)
        assert valid is False

    def test_low_confidence_fields_not_counted(self):
        doc = make_expense_doc([
            make_field("VENDOR_NAME", "Acme",      45.0),
            make_field("TOTAL",       "$1,000.00",  50.0),
        ])
        valid, reason = is_valid_invoice(doc)
        assert valid is False
        assert "low-confidence" in reason.lower()

    def test_non_invoice_fields_only_fails(self):
        doc = make_expense_doc([
            make_field("UNKNOWN_FIELD", "some value", 99.0),
        ])
        valid, reason = is_valid_invoice(doc)
        assert valid is False


class TestGetOverallConfidence:

    def test_high_confidence_doc(self):
        doc = make_expense_doc([
            make_field("VENDOR_NAME", "Acme",    98.0),
            make_field("TOTAL",       "$500.00", 96.0),
        ])
        assert get_overall_confidence(doc) > 90.0

    def test_empty_doc_returns_zero(self):
        assert get_overall_confidence(make_expense_doc()) == 0.0

    def test_empty_value_fields_excluded(self):
        doc_with    = make_expense_doc([make_field("VENDOR_NAME", "Acme", 98.0),
                                        make_field("TOTAL", "", 10.0)])
        doc_without = make_expense_doc([make_field("VENDOR_NAME", "Acme", 98.0)])
        assert get_overall_confidence(doc_with) == get_overall_confidence(doc_without)

    def test_score_between_0_and_100(self):
        doc = make_expense_doc([make_field("VENDOR_NAME", "Corp", 75.0)])
        assert 0.0 <= get_overall_confidence(doc) <= 100.0


class TestParseExpenseDocument:

    def test_all_summary_fields_parsed(self):
        doc = make_expense_doc([
            make_field("VENDOR_NAME",          "Acme Corp"),
            make_field("TOTAL",                "$1,250.00"),
            make_field("SUBTOTAL",             "$1,150.00"),
            make_field("TAX",                  "$100.00"),
            make_field("INVOICE_RECEIPT_ID",   "INV-001"),
            make_field("INVOICE_RECEIPT_DATE", "2024-03-01"),
            make_field("DUE_DATE",             "2024-04-01"),
        ])
        inv = parse_expense_document(doc, "bucket", "submitted-invoices/inv.jpg", 0)
        assert inv["vendorName"]    == "Acme Corp"
        assert inv["totalAmount"]   == "$1,250.00"
        assert inv["subtotal"]      == "$1,150.00"
        assert inv["tax"]           == "$100.00"
        assert inv["invoiceNumber"] == "INV-001"
        assert inv["invoiceDate"]   == "2024-03-01"
        assert inv["dueDate"]       == "2024-04-01"

    def test_invoice_id_set(self):
        inv = parse_expense_document(make_expense_doc(), "b", "k.jpg", 0)
        assert inv["invoiceId"] != ""

    def test_confidence_score_stored(self):
        doc = make_expense_doc([make_field("VENDOR_NAME", "Acme", 90.0)])
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert float(inv["confidenceScore"]) > 0

    def test_line_items_parsed(self):
        doc = make_expense_doc(
            summary_fields=[make_field("VENDOR_NAME", "Corp", 95.0)],
            line_item_groups=[{"LineItems": [make_line_item(
                ("ITEM", "Widget A"), ("QUANTITY", "5"),
                ("UNIT_PRICE", "$50.00"), ("PRICE", "$250.00"),
            )]}]
        )
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["lineItemCount"] == 1
        assert inv["lineItems"][0]["description"] == "Widget A"

    def test_higher_confidence_wins_for_duplicate_keys(self):
        doc = make_expense_doc([
            make_field("TOTAL",       "$1,000.00", 99.0),
            make_field("AMOUNT_PAID", "$900.00",   55.0),
        ])
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["totalAmount"] == "$1,000.00"

    def test_amount_paid_fallback(self):
        doc = make_expense_doc([make_field("AMOUNT_PAID", "$500.00", 95.0)])
        inv = parse_expense_document(doc, "b", "k.pdf", 0)
        assert inv["totalAmount"] == "$500.00"

    def test_ttl_is_in_future(self):
        import time
        inv = parse_expense_document(make_expense_doc(), "b", "k.jpg", 0)
        assert inv["ttl"] > int(time.time())


# ═════════════════════════════════════════════════════════════════════════════
# PATH B — AnalyzeDocument Tests
# ═════════════════════════════════════════════════════════════════════════════

class TestIsValidInvoiceFromBlocks:

    def test_valid_invoice_passes(self):
        blocks = make_kv_blocks({
            "Invoice #":    "INV-001",
            "Vendor Name":  "Acme Corp",
            "Total":        "$500.00",
        })
        valid, reason = is_valid_invoice_from_blocks(blocks)
        assert valid is True
        assert reason == ""

    def test_empty_blocks_fails(self):
        valid, reason = is_valid_invoice_from_blocks([])
        assert valid is False
        assert "no blocks" in reason.lower()

    def test_no_kv_pairs_fails(self):
        # Only WORD blocks — no KEY_VALUE_SET
        blocks = [{"Id": "1", "BlockType": "WORD", "Text": "hello", "Relationships": []}]
        valid, reason = is_valid_invoice_from_blocks(blocks)
        assert valid is False

    def test_non_invoice_kv_fails(self):
        blocks = make_kv_blocks({
            "Party A":    "John Doe",
            "Party B":    "Jane Smith",
            "Clause 1":   "Agreement terms here",
        })
        valid, reason = is_valid_invoice_from_blocks(blocks)
        assert valid is False

    def test_single_invoice_field_fails(self):
        blocks = make_kv_blocks({"Total": "$500.00"})
        valid, reason = is_valid_invoice_from_blocks(blocks)
        assert valid is False


class TestGetBlocksConfidence:

    def test_returns_average(self):
        blocks = [
            {"Id": "1", "BlockType": "WORD",          "Text": "hello", "Confidence": 90.0, "Relationships": []},
            {"Id": "2", "BlockType": "KEY_VALUE_SET",  "Text": "world", "Confidence": 80.0, "Relationships": [], "EntityTypes": ["KEY"]},
        ]
        score = get_blocks_confidence(blocks)
        assert score == 85.0

    def test_empty_blocks_returns_zero(self):
        assert get_blocks_confidence([]) == 0.0

    def test_excludes_empty_text_blocks(self):
        blocks = [
            {"Id": "1", "BlockType": "WORD", "Text": "hello", "Confidence": 90.0, "Relationships": []},
            {"Id": "2", "BlockType": "WORD", "Text": "",       "Confidence": 10.0, "Relationships": []},
        ]
        score = get_blocks_confidence(blocks)
        assert score == 90.0


class TestParseDocumentBlocks:

    def test_vendor_and_total_parsed(self):
        blocks = make_kv_blocks({
            "Vendor Name": "Digital Corp",
            "Invoice #":   "INV-999",
            "Total":       "$750.00",
            "Invoice Date":"2024-06-01",
        })
        inv = parse_document_blocks(blocks, "bucket", "submitted-invoices/doc.pdf")
        assert inv["vendorName"]    == "Digital Corp"
        assert inv["invoiceNumber"] == "INV-999"
        assert inv["totalAmount"]   == "$750.00"
        assert inv["invoiceDate"]   == "2024-06-01"

    def test_invoice_id_always_set(self):
        inv = parse_document_blocks([], "b", "k.pdf")
        assert inv["invoiceId"] != ""

    def test_timestamp_set(self):
        inv = parse_document_blocks([], "b", "k.pdf")
        assert "T" in inv["timestamp"]

    def test_ttl_in_future(self):
        import time
        inv = parse_document_blocks([], "b", "k.pdf")
        assert inv["ttl"] > int(time.time())

    def test_various_label_synonyms(self):
        """Test that synonym labels map correctly to DB keys."""
        blocks = make_kv_blocks({
            "Supplier":     "Vendor X",
            "Amount Due":   "$200.00",
            "Bill To":      "Client Y",
            "Due Date":     "2024-07-01",
        })
        inv = parse_document_blocks(blocks, "b", "k.pdf")
        assert inv.get("vendorName")    == "Vendor X"
        assert inv.get("totalAmount")   == "$200.00"
        assert inv.get("receiverName")  == "Client Y"
        assert inv.get("dueDate")       == "2024-07-01"

    def test_confidence_score_stored(self):
        blocks = make_kv_blocks({"Total": "$100.00"})
        inv = parse_document_blocks(blocks, "b", "k.pdf")
        assert "confidenceScore" in inv


# ═════════════════════════════════════════════════════════════════════════════
# SHARED HELPER TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestHelpers:

    def test_get_field_type_uppercase(self):
        assert _get_field_type({"Type": {"Text": "vendor_name"}}) == "VENDOR_NAME"

    def test_get_field_type_none(self):
        assert _get_field_type({}) == ""
        assert _get_field_type({"Type": None}) == ""

    def test_get_field_value_strips(self):
        assert _get_field_value({"ValueDetection": {"Text": "  Acme  "}}) == "Acme"

    def test_get_field_value_none(self):
        assert _get_field_value({}) == ""

    def test_get_confidence_float(self):
        assert _get_confidence({"ValueDetection": {"Confidence": 97.5}}) == 97.5

    def test_get_confidence_default_zero(self):
        assert _get_confidence({}) == 0.0

    def test_extract_key_value_pairs_builds_dict(self):
        blocks = make_kv_blocks({"Invoice #": "INV-001", "Total": "$500"})
        pairs  = _extract_key_value_pairs(blocks)
        assert pairs.get("Invoice #") == "INV-001"
        assert pairs.get("Total")     == "$500"
