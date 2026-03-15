"""
invoice_processor — AWS Lambda (Python 3.14)
=============================================
Serverless invoice processing pipeline with two-path Textract routing.

Path A — Scanned / image-based documents (JPG, PNG, scanned PDF):
  Textract AnalyzeExpense  →  structured expense fields

Path B — Digitally created PDFs (Word → PDF, Excel → PDF):
  Textract AnalyzeDocument →  FORMS + TABLES key-value extraction

S3 Folder Flow:
  submitted-invoices/  →  Lambda  →  Textract (Path A or B)
      ├── confidence >= 80%  →  DynamoDB (auto-processed)
      └── confidence <  80%  →  invoices-to-be-reviewed/  +  SNS alert
"""

import os
import logging
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

from extractor import (
    parse_expense_document,
    parse_document_blocks,
    get_overall_confidence,
    get_blocks_confidence,
    is_valid_invoice,
    is_valid_invoice_from_blocks,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients ───────────────────────────────────────────────────────────────
_textract = boto3.client("textract")
_dynamodb = boto3.resource("dynamodb")
_s3       = boto3.client("s3")
_sns      = boto3.client("sns")

# ── Config ────────────────────────────────────────────────────────────────────
TABLE_NAME           = os.environ.get("DYNAMODB_TABLE",        "InvoiceExpenses")
SUBMIT_FOLDER        = os.environ.get("SUBMIT_FOLDER",         "submitted-invoices")
REVIEW_FOLDER        = os.environ.get("REVIEW_FOLDER",         "invoices-to-be-reviewed")
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "80.0"))
SNS_TOPIC_ARN        = os.environ.get("SNS_TOPIC_ARN",         "")

# Textract error codes that mean "bad document" — handle gracefully, not crash
GRACEFUL_TEXTRACT_ERRORS = {
    "UnsupportedDocumentException",
    "BadDocumentException",
    "InvalidS3ObjectException",
    "ProvisionedThroughputExceededException",
}

_table = _dynamodb.Table(TABLE_NAME)


# ── Lambda Entry Point ────────────────────────────────────────────────────────
def lambda_handler(event: dict, context) -> dict:
    """
    Main handler triggered by S3 ObjectCreated events.

    For each uploaded file:
      1. Try Textract AnalyzeExpense (Path A — scanned / image-based docs)
      2. If UnsupportedDocumentException → fallback to AnalyzeDocument (Path B — digital PDFs)
      3. Validate extracted data looks like an invoice
      4a. Confidence >= threshold  →  save to DynamoDB
      4b. Confidence <  threshold  →  move to review folder + SNS alert
    """
    processed       = []
    moved_to_review = []
    errors          = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = unquote_plus(record["s3"]["object"]["key"])

        # Guard — only process files from the submit folder
        if not key.startswith(f"{SUBMIT_FOLDER}/"):
            logger.info("⏭️  Skipping %s — not in %s/", key, SUBMIT_FOLDER)
            continue

        # Guard — ignore folder-creation events
        if key.endswith("/"):
            continue

        logger.info("📄 Processing: s3://%s/%s", bucket, key)

        try:
            result = _extract_invoice_data(bucket, key)

            if result is None:
                # Unrecoverable extraction failure — already moved to review
                moved_to_review.append(key)
                continue

            invoice, confidence, extraction_path = result

            logger.info(
                "📊 Path=%s | Confidence=%.1f%% | Threshold=%.1f%%",
                extraction_path, confidence, CONFIDENCE_THRESHOLD,
            )

            if confidence >= CONFIDENCE_THRESHOLD:
                # ── HIGH CONFIDENCE → DynamoDB ────────────────────────────────
                _table.put_item(Item=invoice)
                logger.info(
                    "✅ Saved invoice %s (path=%s, confidence=%.1f%%)",
                    invoice["invoiceId"], extraction_path, confidence,
                )
                processed.append({
                    "key":             key,
                    "invoiceId":       invoice["invoiceId"],
                    "confidence":      confidence,
                    "extractionPath":  extraction_path,
                })

            else:
                # ── LOW CONFIDENCE → review folder + SNS ─────────────────────
                reason  = (
                    f"Confidence {confidence:.1f}% is below "
                    f"threshold {CONFIDENCE_THRESHOLD}% "
                    f"(extraction path: {extraction_path})"
                )
                new_key = _move_to_review(bucket, key, reason)
                moved_to_review.append(key)
                if SNS_TOPIC_ARN:
                    _send_review_alert(
                        bucket, key, new_key, confidence, invoice,
                        extraction_path=extraction_path,
                    )

        except Exception as exc:
            msg = f"❌ Failed for s3://{bucket}/{key}: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)

    if errors:
        raise RuntimeError(f"{len(errors)} record(s) failed:\n" + "\n".join(errors))

    logger.info(
        "🏁 Done — processed: %d | sent to review: %d",
        len(processed), len(moved_to_review),
    )
    return {
        "statusCode":    200,
        "processed":     processed,
        "movedToReview": moved_to_review,
    }


# ── Two-Path Textract Extraction ──────────────────────────────────────────────
def _extract_invoice_data(
    bucket: str,
    key:    str,
) -> tuple[dict, float, str] | None:
    """
    Attempts invoice data extraction using a two-path strategy:

    Path A — AnalyzeExpense (scanned/image-based docs):
        Best for photos, scanned PDFs, JPG/PNG invoices.
        Returns structured expense fields directly.

    Path B — AnalyzeDocument (digital PDFs):
        Fallback when AnalyzeExpense raises UnsupportedDocumentException.
        Uses FORMS + TABLES feature to extract key-value pairs and tables
        from digitally generated PDFs (e.g. Word → PDF).

    Returns:
        (invoice_dict, confidence_score, path_label) on success
        None if the document could not be processed — file moved to review
    """

    # ── PATH A: AnalyzeExpense ────────────────────────────────────────────────
    try:
        logger.info("🔍 Path A — AnalyzeExpense for %s", key)
        response     = _textract.analyze_expense(
            Document={"S3Object": {"Bucket": bucket, "Name": key}}
        )
        expense_docs = response.get("ExpenseDocuments", [])

        if not expense_docs:
            logger.warning("⚠️  AnalyzeExpense returned no documents for %s", key)
            reason = "Textract AnalyzeExpense returned no expense documents"
            _move_to_review(bucket, key, reason)
            if SNS_TOPIC_ARN:
                _send_review_alert(bucket, key,
                                   f"{REVIEW_FOLDER}/{key.split('/')[-1]}",
                                   0.0, {}, extra_reason=reason)
            return None

        # Use first expense document (single-page invoices)
        expense_doc = expense_docs[0]

        # Validate it looks like an invoice
        valid, reason = is_valid_invoice(expense_doc)
        if not valid:
            logger.warning("⛔ Gate 1 failed (Path A) — %s: %s", key, reason)
            new_key = _move_to_review(bucket, key, reason)
            if SNS_TOPIC_ARN:
                _send_review_alert(bucket, key, new_key, 0.0, {},
                                   extra_reason=reason)
            return None

        confidence = get_overall_confidence(expense_doc)
        invoice    = parse_expense_document(expense_doc, bucket, key, 0)
        invoice["extractionPath"] = "A-AnalyzeExpense"
        return invoice, confidence, "A-AnalyzeExpense"

    except ClientError as e:
        error_code = e.response["Error"]["Code"]

        if error_code == "UnsupportedDocumentException":
            # Expected for digital PDFs — fall through to Path B
            logger.info(
                "↩️  Path A unsupported for %s — falling back to Path B (AnalyzeDocument)",
                key,
            )
        elif error_code in GRACEFUL_TEXTRACT_ERRORS:
            # Other known rejections — move to review, don't crash
            reason = f"Textract rejected file ({error_code}): {e.response['Error']['Message']}"
            logger.warning("⛔ %s — %s", key, reason)
            new_key = _move_to_review(bucket, key, reason)
            if SNS_TOPIC_ARN:
                _send_review_alert(bucket, key, new_key, 0.0, {},
                                   extra_reason=reason)
            return None
        else:
            raise   # unexpected AWS error — let outer handler catch it

    # ── PATH B: AnalyzeDocument ───────────────────────────────────────────────
    try:
        logger.info("🔍 Path B — AnalyzeDocument for %s", key)
        response = _textract.analyze_document(
            Document={"S3Object": {"Bucket": bucket, "Name": key}},
            FeatureTypes=["TABLES", "FORMS"],
        )
        blocks = response.get("Blocks", [])

        if not blocks:
            reason = "Textract AnalyzeDocument returned no blocks"
            logger.warning("⚠️  %s — %s", key, reason)
            new_key = _move_to_review(bucket, key, reason)
            if SNS_TOPIC_ARN:
                _send_review_alert(bucket, key, new_key, 0.0, {},
                                   extra_reason=reason)
            return None

        # Validate it looks like an invoice
        valid, reason = is_valid_invoice_from_blocks(blocks)
        if not valid:
            logger.warning("⛔ Gate 1 failed (Path B) — %s: %s", key, reason)
            new_key = _move_to_review(bucket, key, reason)
            if SNS_TOPIC_ARN:
                _send_review_alert(bucket, key, new_key, 0.0, {},
                                   extra_reason=reason)
            return None

        confidence = get_blocks_confidence(blocks)
        invoice    = parse_document_blocks(blocks, bucket, key)
        invoice["extractionPath"] = "B-AnalyzeDocument"
        return invoice, confidence, "B-AnalyzeDocument"

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code in GRACEFUL_TEXTRACT_ERRORS:
            reason = f"Path B also failed ({error_code}): {e.response['Error']['Message']}"
            logger.warning("⛔ %s — %s", key, reason)
            new_key = _move_to_review(bucket, key, reason)
            if SNS_TOPIC_ARN:
                _send_review_alert(bucket, key, new_key, 0.0, {},
                                   extra_reason=reason)
            return None
        raise


# ── S3 Move Helper ────────────────────────────────────────────────────────────
def _move_to_review(bucket: str, source_key: str, reason: str) -> str:
    """
    Copies file from submitted-invoices/ to invoices-to-be-reviewed/
    with rejection reason stored as S3 object metadata, then deletes original.
    Returns the new S3 key.
    """
    filename = source_key.split("/")[-1]
    dest_key = f"{REVIEW_FOLDER}/{filename}"

    _s3.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": source_key},
        Key=dest_key,
        Metadata={
            "review-reason":   reason[:256],
            "original-s3-key": source_key,
        },
        MetadataDirective="REPLACE",
    )
    _s3.delete_object(Bucket=bucket, Key=source_key)

    logger.info("📁 Moved  %s  →  %s", source_key, dest_key)
    return dest_key


# ── SNS Alert Helper ──────────────────────────────────────────────────────────
def _send_review_alert(
    bucket:          str,
    original_key:    str,
    review_key:      str,
    confidence:      float,
    invoice:         dict,
    extraction_path: str = "",
    extra_reason:    str = "",
) -> None:
    """
    Publishes SNS email alert with full context for the human reviewer.
    SNS failures are caught and logged — never crashes the Lambda.
    """
    filename = original_key.split("/")[-1]
    vendor   = invoice.get("vendorName",    "Not detected")
    total    = invoice.get("totalAmount",   "Not detected")
    inv_date = invoice.get("invoiceDate",   "Not detected")
    inv_num  = invoice.get("invoiceNumber", "Not detected")

    if extra_reason:
        confidence_line = f"  Reason          : {extra_reason}"
    else:
        confidence_line = (
            f"  Confidence      : {confidence:.1f}%  "
            f"(threshold: {CONFIDENCE_THRESHOLD}%)"
        )

    path_line = (
        f"  Extraction Path : {extraction_path}"
        if extraction_path else ""
    )

    subject = f"[Invoice Review Required] {filename}"
    message = f"""
An invoice could not be automatically processed and requires human review.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  FILE DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  File Name       : {filename}
  S3 Location     : s3://{bucket}/{review_key}
{confidence_line}
{path_line}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  EXTRACTED DATA (partial / low confidence)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Vendor          : {vendor}
  Total Amount    : {total}
  Invoice Date    : {inv_date}
  Invoice No.     : {inv_num}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ACTION REQUIRED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. Open the file : s3://{bucket}/{review_key}
  2. Verify invoice details manually
  3. Enter data into DynamoDB table '{TABLE_NAME}' if valid
  4. Delete the file from the review folder once done

This is an automated alert from the Invoice Processor Lambda.
"""

    try:
        _sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],
            Message=message,
        )
        logger.info("📧 SNS alert sent for %s", filename)
    except Exception as exc:
        logger.error("⚠️  SNS publish failed for %s: %s", filename, exc)
