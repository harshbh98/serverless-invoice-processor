"""
invoice_processor — AWS Lambda (Python 3.14)
=============================================
Serverless invoice processing pipeline.

S3 Folder Flow:
  submitted-invoices/  →  Lambda  →  Textract AnalyzeExpense
      ├── confidence >= 80%  →  DynamoDB (auto-processed)
      └── confidence <  80%  →  invoices-to-be-reviewed/  +  SNS alert

Supported file types: JPG, PNG, PDF, TIFF
"""

import os
import logging
from urllib.parse import unquote_plus
import boto3
from extractor import parse_expense_document, get_overall_confidence, is_valid_invoice

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients (initialised once on cold start) ──────────────────────────────
_textract = boto3.client("textract")
_dynamodb = boto3.resource("dynamodb")
_s3       = boto3.client("s3")
_sns      = boto3.client("sns")

# ── Configuration from Lambda environment variables ───────────────────────────
TABLE_NAME            = os.environ.get("DYNAMODB_TABLE",        "InvoiceExpenses")
SUBMIT_FOLDER         = os.environ.get("SUBMIT_FOLDER",         "submitted-invoices")
REVIEW_FOLDER         = os.environ.get("REVIEW_FOLDER",         "invoices-to-be-reviewed")
CONFIDENCE_THRESHOLD  = float(os.environ.get("CONFIDENCE_THRESHOLD", "80.0"))
SNS_TOPIC_ARN         = os.environ.get("SNS_TOPIC_ARN",         "")

_table = _dynamodb.Table(TABLE_NAME)


# ── Lambda Entry Point ────────────────────────────────────────────────────────
def lambda_handler(event: dict, context) -> dict:
    """
    Main handler — triggered by S3 ObjectCreated events from submitted-invoices/.

    For each uploaded file:
      1. Call Textract AnalyzeExpense
      2. Validate it is actually an invoice
      3a. Confidence >= threshold  →  parse + save to DynamoDB
      3b. Confidence <  threshold  →  move to review folder + SNS alert

    Args:
        event:   AWS S3 event payload
        context: Lambda runtime context (unused)

    Returns:
        dict summarising processed and reviewed file counts
    """
    processed       = []
    moved_to_review = []
    errors          = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = unquote_plus(record["s3"]["object"]["key"])

        # Guard — only handle files from the submit folder
        if not key.startswith(f"{SUBMIT_FOLDER}/"):
            logger.info("⏭️  Skipping %s — not in %s/", key, SUBMIT_FOLDER)
            continue

        # Guard — ignore folder-creation events (keys ending with /)
        if key.endswith("/"):
            continue

        logger.info("📄 Processing: s3://%s/%s", bucket, key)

        try:
            # ── Step 1: Textract ──────────────────────────────────────────────
            response     = _textract.analyze_expense(
                Document={"S3Object": {"Bucket": bucket, "Name": key}}
            )
            expense_docs = response.get("ExpenseDocuments", [])

            if not expense_docs:
                logger.warning("⚠️  Textract found no expense data in %s", key)
                new_key = _move_to_review(bucket, key, "No expense data detected by Textract")
                moved_to_review.append(key)
                if SNS_TOPIC_ARN:
                    _send_review_alert(bucket, key, new_key, 0.0, {})
                continue

            for idx, expense_doc in enumerate(expense_docs):

                # ── Step 2: Validate it looks like an invoice ─────────────────
                valid, validation_reason = is_valid_invoice(expense_doc)
                if not valid:
                    logger.warning("⛔ Not an invoice — %s: %s", key, validation_reason)
                    new_key = _move_to_review(bucket, key, validation_reason)
                    moved_to_review.append(key)
                    if SNS_TOPIC_ARN:
                        _send_review_alert(bucket, key, new_key, 0.0, expense_doc,
                                           extra_reason=validation_reason)
                    continue

                # ── Step 3: Confidence gate ───────────────────────────────────
                confidence = get_overall_confidence(expense_doc)
                logger.info("📊 Confidence: %.1f%% | Threshold: %.1f%%",
                            confidence, CONFIDENCE_THRESHOLD)

                if confidence >= CONFIDENCE_THRESHOLD:
                    # ── HIGH CONFIDENCE → DynamoDB ────────────────────────────
                    invoice = parse_expense_document(expense_doc, bucket, key, idx)
                    _table.put_item(Item=invoice)
                    logger.info("✅ Saved invoice %s (confidence %.1f%%)",
                                invoice["invoiceId"], confidence)
                    processed.append({
                        "key":        key,
                        "invoiceId":  invoice["invoiceId"],
                        "confidence": confidence,
                    })

                else:
                    # ── LOW CONFIDENCE → review folder + SNS ──────────────────
                    reason  = (f"Confidence {confidence:.1f}% is below "
                               f"threshold {CONFIDENCE_THRESHOLD}%")
                    new_key = _move_to_review(bucket, key, reason)
                    moved_to_review.append(key)
                    logger.warning("📋 Moved to review: %s (%s)", key, reason)

                    if SNS_TOPIC_ARN:
                        _send_review_alert(bucket, key, new_key, confidence, expense_doc)

        except Exception as exc:
            msg = f"❌ Failed for s3://{bucket}/{key}: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)

    if errors:
        raise RuntimeError(f"{len(errors)} record(s) failed:\n" + "\n".join(errors))

    logger.info(
        "🏁 Done — processed: %d | sent to review: %d",
        len(processed), len(moved_to_review)
    )
    return {
        "statusCode":     200,
        "processed":      processed,
        "movedToReview":  moved_to_review,
    }


# ── S3 Move Helper ────────────────────────────────────────────────────────────
def _move_to_review(bucket: str, source_key: str, reason: str) -> str:
    """
    Copies the file from submitted-invoices/ to invoices-to-be-reviewed/
    preserving the filename, then deletes the original.

    Attaches the rejection reason as S3 object metadata so reviewers
    can see it in the AWS console without opening the file.

    Returns the new S3 key.
    """
    filename = source_key.split("/")[-1]
    dest_key = f"{REVIEW_FOLDER}/{filename}"

    # Copy with metadata
    _s3.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": source_key},
        Key=dest_key,
        Metadata={
            "review-reason":    reason[:256],        # S3 metadata max 2KB per key
            "original-s3-key":  source_key,
        },
        MetadataDirective="REPLACE",
    )

    # Delete from submit folder
    _s3.delete_object(Bucket=bucket, Key=source_key)

    logger.info("📁 Moved  %s  →  %s", source_key, dest_key)
    return dest_key


# ── SNS Alert Helper ──────────────────────────────────────────────────────────
def _send_review_alert(
    bucket:       str,
    original_key: str,
    review_key:   str,
    confidence:   float,
    expense_doc:  dict,
    extra_reason: str = "",
) -> None:
    """
    Publishes an email alert via SNS so the human reviewer is notified
    immediately with full context — filename, confidence score, and
    whatever partial data Textract managed to extract.
    """
    filename = original_key.split("/")[-1]
    vendor   = _get_field(expense_doc, "VENDOR_NAME")
    total    = _get_field(expense_doc, "TOTAL") or _get_field(expense_doc, "AMOUNT_PAID")
    inv_date = _get_field(expense_doc, "INVOICE_RECEIPT_DATE")
    inv_num  = _get_field(expense_doc, "INVOICE_RECEIPT_ID")

    if extra_reason:
        rejection_line = f"  Reason       : {extra_reason}"
    else:
        rejection_line = (f"  Confidence   : {confidence:.1f}%  "
                          f"(threshold: {CONFIDENCE_THRESHOLD}%)")

    subject = f"[Invoice Review Required] {filename}"
    message = f"""
An invoice could not be automatically processed and requires human review.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  FILE DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  File Name    : {filename}
  S3 Location  : s3://{bucket}/{review_key}
{rejection_line}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  EXTRACTED DATA (partial / low confidence)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Vendor       : {vendor   or 'Not detected'}
  Total Amount : {total    or 'Not detected'}
  Invoice Date : {inv_date or 'Not detected'}
  Invoice No.  : {inv_num  or 'Not detected'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ACTION REQUIRED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1. Open the file:  s3://{bucket}/{review_key}
  2. Verify the invoice details manually
  3. Enter data into DynamoDB table '{TABLE_NAME}' if valid
  4. Delete the file from the review folder once done

This is an automated alert from the Invoice Processor Lambda function.
"""

    try:
        _sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],          # SNS subject max 100 chars
            Message=message,
        )
        logger.info("📧 SNS alert sent for %s", filename)
    except Exception as exc:
        # Never let SNS failure crash the Lambda — just log it
        logger.error("⚠️  SNS publish failed for %s: %s", filename, exc)


# ── Field extraction helper ───────────────────────────────────────────────────
def _get_field(expense_doc: dict, field_type: str) -> str:
    """Pull a single field value from an expense doc summary."""
    for field in expense_doc.get("SummaryFields", []):
        if (field.get("Type") or {}).get("Text", "").upper() == field_type:
            return ((field.get("ValueDetection") or {}).get("Text") or "").strip()
    return ""
