# serverless-invoice-processor

> Automatically extract and store invoice data using AWS Lambda, Amazon Textract, S3, and DynamoDB.  
> Confidence-gated Human-in-the-Loop (HITL) pipeline with two-path Textract routing —  
> supports both scanned documents and digitally created PDFs.

![Python](https://img.shields.io/badge/Python-3.14-blue)
![Runtime](https://img.shields.io/badge/Runtime-AWS%20Lambda-orange)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Two-Path Textract Routing](#3-two-path-textract-routing)
4. [S3 Folder Structure](#4-s3-folder-structure)
5. [Confidence-Gated Flow](#5-confidence-gated-flow)
6. [Project Structure](#6-project-structure)
7. [Prerequisites](#7-prerequisites)
8. [Local Development Setup](#8-local-development-setup)
9. [AWS Infrastructure Setup](#9-aws-infrastructure-setup)
10. [Deploying to Lambda](#10-deploying-to-lambda)
11. [Connecting the S3 Trigger](#11-connecting-the-s3-trigger)
12. [SNS Email Alerts Setup](#12-sns-email-alerts-setup)
13. [Environment Variables](#13-environment-variables)
14. [Lambda Configuration](#14-lambda-configuration)
15. [IAM Permissions](#15-iam-permissions)
16. [DynamoDB Schema](#16-dynamodb-schema)
17. [Testing](#17-testing)
18. [Troubleshooting](#18-troubleshooting)
19. [Security Best Practices](#19-security-best-practices)
20. [License](#20-license)

---

## 1. Overview

This project implements a **fully serverless, confidence-gated invoice processing pipeline** on AWS.

When a user uploads an invoice image or PDF to the `submitted-invoices/` folder in S3:

- **Path A** (scanned / image-based): Textract `AnalyzeExpense` extracts structured expense fields
- **Path B** (digitally created PDFs): If Path A fails, automatically falls back to `AnalyzeDocument` using FORMS + TABLES
- A **weighted confidence score** is calculated across all extracted fields
- **High confidence (≥ 80%)** → data saved automatically to **DynamoDB**
- **Low confidence (< 80%)** → file moved to `invoices-to-be-reviewed/` + **SNS email alert**

No servers, no cron jobs, no polling — entirely event-driven.

---

## 2. Architecture

```
User
 │
 │  (1) Upload invoice to submitted-invoices/
 ▼
Amazon S3 (Source Bucket)
 │
 │  (2) S3 ObjectCreated event trigger
 ▼
AWS Lambda
 │
 │  (3) Path A: Textract AnalyzeExpense (scanned / image-based)
 │       └── UnsupportedDocumentException?
 │             └── (3b) Path B: Textract AnalyzeDocument (digital PDF)
 │
 │  (4) Gate 1: is_valid_invoice() — is this actually an invoice?
 │  (5) Gate 2: get_overall_confidence() — is the quality good enough?
 │
 ├── Confidence >= 80% ─────────────────────────────────► DynamoDB ✅
 │
 └── Confidence < 80%  ──┬──────────────────────────────► invoices-to-be-reviewed/ 📁
                         └──────────────────────────────► SNS email alert 📧
```

---

## 3. Two-Path Textract Routing

This is the core differentiator from a standard Textract integration.

| | Path A | Path B |
|---|---|---|
| **Textract API** | `AnalyzeExpense` | `AnalyzeDocument` |
| **Best for** | Scanned invoices, receipt photos, image PDFs | Digitally created PDFs (Word → PDF, Excel → PDF) |
| **Input formats** | JPG, PNG, TIFF, scanned PDF | Digital PDF |
| **Returns** | Structured expense fields (VENDOR_NAME, TOTAL etc.) | FORMS key-value pairs + TABLE rows |
| **Triggered by** | Default for all uploads | Automatic fallback on `UnsupportedDocumentException` |
| **DynamoDB field** | `extractionPath: A-AnalyzeExpense` | `extractionPath: B-AnalyzeDocument` |

### How the Fallback Works

```python
try:
    # Always try Path A first
    response = textract.analyze_expense(...)

except UnsupportedDocumentException:
    # Digital PDF detected — automatically retry with Path B
    response = textract.analyze_document(
        FeatureTypes=["TABLES", "FORMS"]
    )
```

You don't need to know in advance which type of PDF you're uploading — the pipeline detects and routes automatically.

### Other Textract Errors — Graceful Handling

All other Textract rejection errors are caught and handled without crashing Lambda:

| Error | Cause | Outcome |
|-------|-------|---------|
| `UnsupportedDocumentException` | Digitally created PDF on Path A | Retried on Path B |
| `BadDocumentException` | Corrupt or unreadable file | Moved to review + SNS alert |
| `InvalidS3ObjectException` | Bad key, wrong region, permissions | Moved to review + SNS alert |
| `ProvisionedThroughputExceededException` | Textract rate limit hit | Moved to review + SNS alert |

---

## 4. S3 Folder Structure

```
your-invoice-bucket/
├── submitted-invoices/        ← Users upload files HERE
│   └── invoice-001.jpg
│
└── invoices-to-be-reviewed/   ← Low-confidence / rejected files land HERE
    └── invoice-002.pdf        (+ reviewer gets SNS email alert)
```

**Supported file types:** JPG, PNG, TIFF, PDF (scanned or digital)

---

## 5. Confidence-Gated Flow

### Gate 1 — Invoice Validation (Semantic Check)

At least **2 of the following fields** must be detected with **≥ 70% per-field confidence**:

`VENDOR_NAME` · `TOTAL` · `SUBTOTAL` · `INVOICE_RECEIPT_ID` · `INVOICE_RECEIPT_DATE` · `AMOUNT_PAID`

> **Note:** The 70% threshold is a *per-field minimum* — it determines whether an individual
> field counts as detected. The 80% threshold in Gate 2 is the *overall weighted average*
> across all fields. These are two separate checks answering two different questions:
> Gate 1 asks "is this an invoice?" and Gate 2 asks "was it read clearly enough?"

### Gate 2 — Confidence Scoring (Quality Check)

| Field Group | Weight | Fields |
|-------------|--------|--------|
| High-value fields | **2×** | `VENDOR_NAME`, `TOTAL`, `AMOUNT_PAID`, `INVOICE_RECEIPT_ID` |
| All other fields | **1×** | `TAX`, `SUBTOTAL`, `DUE_DATE`, `VENDOR_ADDRESS`, etc. |
| Empty / undetected | **Excluded** | Fields with no detected value are not penalised |

- **Score ≥ 80%** → saved to DynamoDB automatically
- **Score < 80%** → moved to `invoices-to-be-reviewed/` + SNS email sent

### Routing Decision

| File Uploaded | Gate 1 | Gate 2 | Outcome |
|--------------|--------|--------|---------|
| Clear scanned invoice | ✅ Pass | ≥ 80% | DynamoDB via Path A |
| Digital PDF invoice | ✅ Pass | ≥ 80% | DynamoDB via Path B |
| Blurry / partial invoice | ✅ Pass | < 80% | Review folder + SNS |
| Contract / legal doc | ❌ Fail | N/A | Review folder + SNS |
| Random photo | ❌ Fail | N/A | Review folder + SNS |
| Corrupt or blank file | ❌ Fail | N/A | Review folder + SNS |

---

## 6. Project Structure

```
serverless-invoice-processor/
├── lambda_function.py    # Lambda entry point
│                         #   Two-path routing, move-to-review, SNS alerts
├── extractor.py          # Textract response parsers
│                         #   Path A: parse_expense_document(), is_valid_invoice()
│                         #   Path B: parse_document_blocks(), is_valid_invoice_from_blocks()
│                         #   Shared: confidence scoring, ID generation
├── test_extractor.py     # pytest unit tests — covers both paths (50+ cases)
├── requirements.txt      # Python dependencies
├── README.md             # This file
└── samples/              # Synthetic test files
    ├── README.md         # Test instructions and expected results
    ├── valid/            # Should pass both gates → DynamoDB
    └── invalid/          # Should fail → invoices-to-be-reviewed/
```

---

## 7. Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.14+ | https://python.org |
| AWS CLI | v2.x | https://aws.amazon.com/cli |
| pip | latest | Bundled with Python |
| pytest | 8.x | `pip install pytest` |

> ⚠️ **Never use the AWS root account.** Create a least-privilege IAM user and enable MFA.

---

## 8. Local Development Setup

```bash
# Clone
git clone https://github.com/your-org/serverless-invoice-processor.git
cd serverless-invoice-processor

# Virtual environment
python3.14 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Configure AWS credentials (IAM user — not root)
aws configure

# Run unit tests
python -m pytest test_extractor.py -v
```

---

## 9. AWS Infrastructure Setup

### 9.1 Create S3 Bucket and Folders

```bash
aws s3api create-bucket \
  --bucket YOUR-UNIQUE-BUCKET-NAME \
  --region us-east-1

aws s3api put-public-access-block \
  --bucket YOUR-UNIQUE-BUCKET-NAME \
  --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,\
      BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-object --bucket YOUR-UNIQUE-BUCKET-NAME --key submitted-invoices/
aws s3api put-object --bucket YOUR-UNIQUE-BUCKET-NAME --key invoices-to-be-reviewed/
```

### 9.2 Create DynamoDB Table

```bash
aws dynamodb create-table \
  --table-name InvoiceExpenses \
  --attribute-definitions \
      AttributeName=invoiceId,AttributeType=S \
      AttributeName=timestamp,AttributeType=S \
  --key-schema \
      AttributeName=invoiceId,KeyType=HASH \
      AttributeName=timestamp,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1

aws dynamodb update-time-to-live \
  --table-name InvoiceExpenses \
  --time-to-live-specification Enabled=true,AttributeName=ttl
```

### 9.3 Create IAM Role for Lambda

```bash
cat > trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name LambdaInvoiceProcessorRole \
  --assume-role-policy-document file://trust-policy.json

aws iam attach-role-policy --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AWSLambdaBasicExecutionRole
aws iam attach-role-policy --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonTextractFullAccess
aws iam attach-role-policy --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
aws iam attach-role-policy --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess
aws iam attach-role-policy --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonSNSFullAccess

aws iam get-role \
  --role-name LambdaInvoiceProcessorRole \
  --query 'Role.Arn' --output text
```

---

## 10. Deploying to Lambda

```bash
# Package — boto3 is pre-installed in Lambda
zip -r function.zip lambda_function.py extractor.py

# First deploy
aws lambda create-function \
  --function-name InvoiceProcessor \
  --runtime python3.14 \
  --role arn:aws:iam::YOUR-ACCOUNT-ID:role/LambdaInvoiceProcessorRole \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://function.zip \
  --timeout 60 \
  --memory-size 256 \
  --environment Variables="{ \
      DYNAMODB_TABLE=InvoiceExpenses, \
      SUBMIT_FOLDER=submitted-invoices, \
      REVIEW_FOLDER=invoices-to-be-reviewed, \
      CONFIDENCE_THRESHOLD=80.0, \
      SNS_TOPIC_ARN=arn:aws:sns:us-east-1:YOUR-ACCOUNT-ID:InvoiceReviewAlerts \
  }" \
  --region us-east-1

# Subsequent deploys
zip -r function.zip lambda_function.py extractor.py
aws lambda update-function-code \
  --function-name InvoiceProcessor \
  --zip-file fileb://function.zip
aws lambda wait function-updated --function-name InvoiceProcessor
```

---

## 11. Connecting the S3 Trigger

```bash
# Grant S3 permission to invoke Lambda
aws lambda add-permission \
  --function-name InvoiceProcessor \
  --statement-id s3-invoke \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn arn:aws:s3:::YOUR-UNIQUE-BUCKET-NAME \
  --source-account YOUR-ACCOUNT-ID

# Attach event notification (submitted-invoices/ prefix only)
cat > notification.json << 'EOF'
{
  "LambdaFunctionConfigurations": [{
    "LambdaFunctionArn": "arn:aws:lambda:us-east-1:YOUR-ACCOUNT-ID:function:InvoiceProcessor",
    "Events": ["s3:ObjectCreated:*"],
    "Filter": {
      "Key": {
        "FilterRules": [
          { "Name": "prefix", "Value": "submitted-invoices/" }
        ]
      }
    }
  }]
}
EOF

aws s3api put-bucket-notification-configuration \
  --bucket YOUR-UNIQUE-BUCKET-NAME \
  --notification-configuration file://notification.json
```

> Files moved to `invoices-to-be-reviewed/` by Lambda do **not** re-trigger the function.

---

## 12. SNS Email Alerts Setup

```bash
aws sns create-topic --name InvoiceReviewAlerts

aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:YOUR-ACCOUNT-ID:InvoiceReviewAlerts \
  --protocol email \
  --notification-endpoint your-email@example.com
```

> You must click the **confirmation link** in the AWS subscription email before alerts will be delivered.

---

## 13. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DYNAMODB_TABLE` | `InvoiceExpenses` | DynamoDB table name |
| `SUBMIT_FOLDER` | `submitted-invoices` | S3 prefix Lambda monitors |
| `REVIEW_FOLDER` | `invoices-to-be-reviewed` | S3 prefix for rejected files |
| `CONFIDENCE_THRESHOLD` | `80.0` | Score below this routes to review |
| `SNS_TOPIC_ARN` | `""` | SNS topic ARN — leave empty to disable alerts |

---

## 14. Lambda Configuration

| Setting | Value | Notes |
|---------|-------|-------|
| Runtime | `python3.14` | Latest managed Python runtime |
| Handler | `lambda_function.lambda_handler` | Do not change |
| Timeout | `60 seconds` | Increase to 120s for large multi-page PDFs |
| Memory | `256 MB` | Increase to 512 MB for very large documents |
| Trigger | S3 `ObjectCreated` | Prefix: `submitted-invoices/` only |

---

## 15. IAM Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET/submitted-invoices/*" },
    { "Effect": "Allow", "Action": ["s3:CopyObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET/invoices-to-be-reviewed/*" },
    { "Effect": "Allow", "Action": ["s3:DeleteObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET/submitted-invoices/*" },
    { "Effect": "Allow", "Action": ["textract:AnalyzeExpense", "textract:AnalyzeDocument"],
      "Resource": "*" },
    { "Effect": "Allow", "Action": ["dynamodb:PutItem"],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT:table/InvoiceExpenses" },
    { "Effect": "Allow", "Action": ["sns:Publish"],
      "Resource": "arn:aws:sns:REGION:ACCOUNT:InvoiceReviewAlerts" },
    { "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "*" }
  ]
}
```

> Note: `textract:AnalyzeDocument` is now required in addition to `textract:AnalyzeExpense` for Path B.

---

## 16. DynamoDB Schema

| Attribute | Type | Key | Description |
|-----------|------|-----|-------------|
| `invoiceId` | String | Partition Key | MD5(s3 path) + millisecond timestamp |
| `timestamp` | String | Sort Key | ISO 8601 UTC processing time |
| `extractionPath` | String | — | `A-AnalyzeExpense` or `B-AnalyzeDocument` |
| `confidenceScore` | String | — | Weighted confidence score (e.g. `94.5`) |
| `s3Bucket` | String | — | Source bucket |
| `s3Key` | String | — | Source object key |
| `vendorName` | String | — | Vendor / supplier name |
| `vendorAddress` | String | — | Vendor address |
| `vendorPhone` | String | — | Vendor phone |
| `invoiceNumber` | String | — | Invoice / receipt ID |
| `invoiceDate` | String | — | Invoice date |
| `dueDate` | String | — | Payment due date |
| `subtotal` | String | — | Pre-tax subtotal |
| `tax` | String | — | Tax amount |
| `totalAmount` | String | — | Total (highest-confidence value wins) |
| `paymentTerms` | String | — | Payment terms (e.g. Net 30) |
| `poNumber` | String | — | Purchase order number |
| `receiverName` | String | — | Billed-to name |
| `lineItems` | List | — | `[{description, quantity, unitPrice, amount}]` |
| `lineItemCount` | Number | — | Count of line items |
| `ttl` | Number | — | Unix epoch — auto-deleted after 1 year |

---

## 17. Testing

See [`samples/README.md`](samples/README.md) for full end-to-end test instructions.

```bash
# Unit tests
python -m pytest test_extractor.py -v --tb=short

# Upload valid samples — expect DynamoDB entries
aws s3 cp samples/valid/ s3://YOUR-BUCKET/submitted-invoices/ --recursive

# Upload invalid samples — expect review folder + SNS emails
aws s3 cp samples/invalid/ s3://YOUR-BUCKET/submitted-invoices/ --recursive

# Watch logs
aws logs tail /aws/lambda/InvoiceProcessor --follow

# Check DynamoDB (note extractionPath column)
aws dynamodb scan \
  --table-name InvoiceExpenses \
  --query 'Items[*].{ID:invoiceId.S,Vendor:vendorName.S,Score:confidenceScore.S,Path:extractionPath.S}' \
  --output table

# Check review folder
aws s3 ls s3://YOUR-BUCKET/invoices-to-be-reviewed/
```

---

## 18. Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Runtime.ImportModuleError` | Wrong handler | Set to `lambda_function.lambda_handler` |
| `AccessDeniedException` (Textract) | Missing IAM policy | Add both `textract:AnalyzeExpense` and `textract:AnalyzeDocument` |
| `AccessDeniedException` (S3) | Missing write permission | Add `s3:CopyObject` + `s3:PutObject` on review prefix |
| `ResourceNotFoundException` (DDB) | Table name mismatch | Check `DYNAMODB_TABLE` env var |
| `UnsupportedDocumentException` (Path B also fails) | Truly unreadable file | File moved to review — check CloudWatch logs for detail |
| `Task timed out` | Large multi-page PDF | Increase Lambda timeout to 120s |
| `ResourceConflictException` on deploy | Update in progress | Run `aws lambda wait function-updated` first |
| SNS alert not received | Email not confirmed | Click the AWS confirmation link in your inbox |
| File stays in submitted-invoices/ | S3 delete permission missing | Add `s3:DeleteObject` on submitted-invoices/* |
| Lambda not triggered | Wrong S3 prefix | Confirm prefix filter is exactly `submitted-invoices/` |

---

## 19. Security Best Practices

- **Never use root account** — least-privilege IAM user for all work
- **Enable MFA** — on root and all IAM users
- **Scope S3 permissions per prefix** — read on `submitted-invoices/`, write on `invoices-to-be-reviewed/`
- **Enable S3 server-side encryption** — SSE-S3 or SSE-KMS for invoice files at rest
- **Enable DynamoDB encryption** — on by default; consider CMK for sensitive data
- **Enable AWS CloudTrail** — audit all API calls
- **Rotate IAM credentials** — prefer short-lived role credentials
- **S3 lifecycle on review folder** — auto-delete after 30 days

---

## 20. License

MIT License — see [LICENSE](LICENSE) for full terms.

---

**Built with:** Python 3.14 · AWS Lambda · Amazon Textract · Amazon S3 · Amazon DynamoDB · Amazon SNS
