# serverless-invoice-processor

> Automatically extract and store invoice data using AWS Lambda, Amazon Textract, S3, and DynamoDB.  
> Confidence-gated Human-in-the-Loop (HITL) pipeline — low-confidence invoices are routed to a review folder with instant SNS email alerts.

![Python](https://img.shields.io/badge/Python-3.14-blue)
![Runtime](https://img.shields.io/badge/Runtime-AWS%20Lambda-orange)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [S3 Folder Structure](#3-s3-folder-structure)
4. [Confidence-Gated Flow](#4-confidence-gated-flow)
5. [Project Structure](#5-project-structure)
6. [Prerequisites](#6-prerequisites)
7. [Local Development Setup](#7-local-development-setup)
8. [AWS Infrastructure Setup](#8-aws-infrastructure-setup)
9. [Deploying to Lambda](#9-deploying-to-lambda)
10. [Connecting the S3 Trigger](#10-connecting-the-s3-trigger)
11. [SNS Email Alerts Setup](#11-sns-email-alerts-setup)
12. [Environment Variables](#12-environment-variables)
13. [Lambda Configuration](#13-lambda-configuration)
14. [IAM Permissions](#14-iam-permissions)
15. [DynamoDB Schema](#15-dynamodb-schema)
16. [Testing](#16-testing)
17. [Troubleshooting](#17-troubleshooting)
18. [Security Best Practices](#18-security-best-practices)
19. [License](#19-license)

---

## 1. Overview

This project implements a **fully serverless, confidence-gated invoice processing pipeline** on AWS.

When a user uploads an invoice image or PDF to the `submitted-invoices/` folder in S3:

- The file is scanned by **Amazon Textract** (`AnalyzeExpense` API)
- The pipeline first checks if the document is actually an **invoice or receipt**
- A **weighted confidence score** is calculated across all extracted fields
- **High confidence (≥ 80%)** → data is saved automatically to **DynamoDB**
- **Low confidence (< 80%)** → file is moved to `invoices-to-be-reviewed/` and a human reviewer is **notified via SNS email**

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
AWS Lambda  ──(3)──►  Amazon Textract (AnalyzeExpense API)
 │
 │  (4) Validate: is it an invoice?
 │  (5) Calculate weighted confidence score
 │
 ├── Confidence >= 80% ─────────────────────────────► Amazon DynamoDB
 │                                                    (auto-processed ✅)
 │
 └── Confidence < 80% ──┬──────────────────────────► invoices-to-be-reviewed/
                        │                            (file moved in S3 📁)
                        │
                        └──────────────────────────► Amazon SNS
                                                     (email alert to reviewer 📧)
```

---

## 3. S3 Folder Structure

```
your-invoice-bucket/
├── submitted-invoices/        ← Users upload files HERE
│   └── invoice-001.jpg
│
└── invoices-to-be-reviewed/   ← Low-confidence files land HERE
    └── invoice-002.pdf        (+ reviewer gets an SNS email alert)
```

| Folder | Purpose |
|--------|---------|
| `submitted-invoices/` | All invoice uploads go here. S3 trigger fires only on this prefix. |
| `invoices-to-be-reviewed/` | Low-confidence or non-invoice files are moved here automatically. |

**Supported file types:** JPG, PNG, PDF, TIFF

---

## 4. Confidence-Gated Flow

### Gate 1 — Invoice Validation (Semantic Check)

Before checking confidence, the pipeline validates the document is actually an invoice.  
At least **2 of the following fields** must be detected with **≥ 70% confidence**:

`VENDOR_NAME` · `TOTAL` · `SUBTOTAL` · `INVOICE_RECEIPT_ID` · `INVOICE_RECEIPT_DATE` · `AMOUNT_PAID`

If this check fails (e.g. someone uploads a contract, photo, or blank page), the file is moved to review.

### Gate 2 — Confidence Scoring (Quality Check)

A **weighted confidence score** is calculated across all detected fields:

| Field Group | Weight | Fields |
|-------------|--------|--------|
| High-value fields | **2×** | `VENDOR_NAME`, `TOTAL`, `AMOUNT_PAID`, `INVOICE_RECEIPT_ID` |
| All other fields | **1×** | `TAX`, `SUBTOTAL`, `DUE_DATE`, `VENDOR_ADDRESS`, etc. |

- **Score ≥ 80%** → saved to DynamoDB automatically
- **Score < 80%** → moved to `invoices-to-be-reviewed/` + SNS email sent

You can tune both thresholds via environment variables (see [Section 12](#12-environment-variables)).

### What Happens to Each Scenario

| File Uploaded | Invoice Check | Confidence | Outcome |
|--------------|--------------|------------|---------|
| Clear invoice scan | ✅ Pass | ≥ 80% | Saved to DynamoDB |
| Blurry / partial invoice | ✅ Pass | < 80% | Moved to review + SNS alert |
| Random photo / contract | ❌ Fail | N/A | Moved to review + SNS alert |
| Completely blank page | ❌ Fail | N/A | Moved to review + SNS alert |
| File in wrong folder | Skipped | N/A | Lambda not triggered |

---

## 5. Project Structure

```
serverless-invoice-processor/
├── lambda_function.py    # Lambda entry point
│                         #   handler: lambda_function.lambda_handler
├── extractor.py          # Textract response parser
│                         #   is_valid_invoice(), get_overall_confidence(),
│                         #   parse_expense_document()
├── test_extractor.py     # pytest unit tests (30+ test cases)
├── requirements.txt      # Python dependencies
└── README.md             # This file
```

---

## 6. Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.14+ | https://python.org |
| AWS CLI | v2.x | https://aws.amazon.com/cli |
| pip | latest | Bundled with Python |
| pytest | 8.x | `pip install pytest` |

> ⚠️ **Never use the AWS root account.** Create a least-privilege IAM user and enable MFA.

---

## 7. Local Development Setup

```bash
# Clone the repo
git clone https://github.com/your-org/serverless-invoice-processor.git
cd serverless-invoice-processor

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Configure AWS credentials (IAM user — not root)
aws configure

# Run unit tests
python -m pytest test_extractor.py -v
```

---

## 8. AWS Infrastructure Setup

Run these commands **once** to provision all required AWS resources.

### 8.1 Create S3 Bucket and Folders

```bash
# Create bucket (replace with a globally unique name)
aws s3api create-bucket \
  --bucket YOUR-UNIQUE-BUCKET-NAME \
  --region us-east-1

# Block all public access
aws s3api put-public-access-block \
  --bucket YOUR-UNIQUE-BUCKET-NAME \
  --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,\
      BlockPublicPolicy=true,RestrictPublicBuckets=true

# Create the two folders (S3 uses zero-byte prefix objects)
aws s3api put-object --bucket YOUR-UNIQUE-BUCKET-NAME --key submitted-invoices/
aws s3api put-object --bucket YOUR-UNIQUE-BUCKET-NAME --key invoices-to-be-reviewed/
```

### 8.2 Create DynamoDB Table

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

# Enable TTL (auto-delete records after 1 year)
aws dynamodb update-time-to-live \
  --table-name InvoiceExpenses \
  --time-to-live-specification Enabled=true,AttributeName=ttl
```

### 8.3 Create IAM Role for Lambda

```bash
# Trust policy
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

# Attach managed policies
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

# Save the ARN for Step 9
aws iam get-role \
  --role-name LambdaInvoiceProcessorRole \
  --query 'Role.Arn' --output text
```

---

## 9. Deploying to Lambda

### 9.1 Package the Deployment ZIP

```bash
# boto3 is pre-installed in Lambda — only zip the source files
zip -r function.zip lambda_function.py extractor.py

# Verify contents
unzip -l function.zip
```

### 9.2 Create the Function (First Deploy Only)

```bash
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
```

### 9.3 Update the Function (Subsequent Deploys)

```bash
zip -r function.zip lambda_function.py extractor.py

aws lambda update-function-code \
  --function-name InvoiceProcessor \
  --zip-file fileb://function.zip \
  --region us-east-1

# Wait for update to complete
aws lambda wait function-updated --function-name InvoiceProcessor
```

---

## 10. Connecting the S3 Trigger

### 10.1 Grant S3 Permission to Invoke Lambda

```bash
aws lambda add-permission \
  --function-name InvoiceProcessor \
  --statement-id s3-invoke \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn arn:aws:s3:::YOUR-UNIQUE-BUCKET-NAME \
  --source-account YOUR-ACCOUNT-ID
```

### 10.2 Attach Event Notification (submitted-invoices/ prefix only)

```bash
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

> The trigger fires **only** on files uploaded to `submitted-invoices/`.  
> Files moved to `invoices-to-be-reviewed/` by Lambda do **not** re-trigger the function.

---

## 11. SNS Email Alerts Setup

```bash
# Create the SNS topic
aws sns create-topic --name InvoiceReviewAlerts

# Subscribe your email address
aws sns subscribe \
  --topic-arn arn:aws:sns:us-east-1:YOUR-ACCOUNT-ID:InvoiceReviewAlerts \
  --protocol email \
  --notification-endpoint your-email@example.com
```

> AWS sends a **confirmation email** after the subscribe command.  
> You must click the confirmation link before alerts will be delivered.

---

## 12. Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `DYNAMODB_TABLE` | `InvoiceExpenses` | No | DynamoDB table name |
| `SUBMIT_FOLDER` | `submitted-invoices` | No | S3 prefix Lambda watches |
| `REVIEW_FOLDER` | `invoices-to-be-reviewed` | No | S3 prefix for low-confidence files |
| `CONFIDENCE_THRESHOLD` | `80.0` | No | Score below this routes file to review |
| `SNS_TOPIC_ARN` | `""` | No | SNS topic ARN for reviewer alerts (leave empty to disable) |

---

## 13. Lambda Configuration

| Setting | Recommended Value | Notes |
|---------|------------------|-------|
| Runtime | `python3.14` | Latest managed Python runtime |
| Handler | `lambda_function.lambda_handler` | Do not change |
| Timeout | `60 seconds` | Increase to 120s for large multi-page PDFs |
| Memory | `256 MB` | Increase to 512 MB for very large documents |
| Architecture | `x86_64` | `arm64` also works for ~20% cost saving |
| Trigger | S3 `ObjectCreated` | Prefix filter: `submitted-invoices/` |

---

## 14. IAM Permissions

Minimum permissions for the Lambda execution role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/submitted-invoices/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:CopyObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/invoices-to-be-reviewed/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:DeleteObject"],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/submitted-invoices/*"
    },
    {
      "Effect": "Allow",
      "Action": ["textract:AnalyzeExpense"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem"],
      "Resource": "arn:aws:dynamodb:REGION:ACCOUNT-ID:table/InvoiceExpenses"
    },
    {
      "Effect": "Allow",
      "Action": ["sns:Publish"],
      "Resource": "arn:aws:sns:REGION:ACCOUNT-ID:InvoiceReviewAlerts"
    },
    {
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "*"
    }
  ]
}
```

---

## 15. DynamoDB Schema

| Attribute | Type | Key | Description |
|-----------|------|-----|-------------|
| `invoiceId` | String | Partition Key | MD5(s3 path) + millisecond timestamp |
| `timestamp` | String | Sort Key | ISO 8601 UTC processing time |
| `s3Bucket` | String | — | Source S3 bucket |
| `s3Key` | String | — | Source S3 object key |
| `confidenceScore` | String | — | Weighted confidence score (e.g. "94.5") |
| `vendorName` | String | — | Extracted vendor name |
| `vendorAddress` | String | — | Vendor address |
| `vendorPhone` | String | — | Vendor phone |
| `invoiceNumber` | String | — | Invoice / receipt ID |
| `invoiceDate` | String | — | Invoice date |
| `dueDate` | String | — | Payment due date |
| `subtotal` | String | — | Pre-tax subtotal |
| `tax` | String | — | Tax amount |
| `totalAmount` | String | — | Total (or AMOUNT_PAID fallback) |
| `paymentTerms` | String | — | Payment terms (e.g. Net 30) |
| `poNumber` | String | — | Purchase order number |
| `lineItems` | List | — | Array of `{description, quantity, unitPrice, amount}` |
| `lineItemCount` | Number | — | Count of line items |
| `ttl` | Number | — | Unix epoch — auto-deleted after 1 year |

---

## 16. Testing

```bash
# Run all unit tests
python -m pytest test_extractor.py -v --tb=short

# Upload a real invoice to test end-to-end
aws s3 cp sample-invoice.jpg s3://YOUR-UNIQUE-BUCKET-NAME/submitted-invoices/

# Watch Lambda logs in real time
aws logs tail /aws/lambda/InvoiceProcessor --follow

# Manually invoke with a fake S3 event
cat > test-event.json << 'EOF'
{
  "Records": [{
    "s3": {
      "bucket": { "name": "YOUR-UNIQUE-BUCKET-NAME" },
      "object": { "key": "submitted-invoices/sample-invoice.jpg" }
    }
  }]
}
EOF

aws lambda invoke \
  --function-name InvoiceProcessor \
  --payload file://test-event.json \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json

# Check DynamoDB for results
aws dynamodb scan \
  --table-name InvoiceExpenses \
  --query 'Items[*].{ID:invoiceId.S,Vendor:vendorName.S,Total:totalAmount.S,Score:confidenceScore.S}' \
  --output table

# Check the review folder
aws s3 ls s3://YOUR-UNIQUE-BUCKET-NAME/invoices-to-be-reviewed/
```

---

## 17. Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Runtime.ImportModuleError` | Wrong handler string | Set handler to `lambda_function.lambda_handler` |
| `AccessDeniedException` (Textract) | Missing IAM policy | Attach `AmazonTextractFullAccess` to role |
| `AccessDeniedException` (S3 CopyObject) | Missing S3 write permission | Add `s3:CopyObject` + `s3:DeleteObject` to role |
| `ResourceNotFoundException` (DynamoDB) | Table name mismatch | Check `DYNAMODB_TABLE` env var |
| `InvalidS3ObjectException` | Unsupported file type | Textract supports JPG, PNG, PDF, TIFF only |
| `Task timed out` | Large multi-page PDF | Increase Lambda timeout to 120s |
| `ResourceConflictException` on deploy | Previous update in progress | Run `aws lambda wait function-updated` first |
| SNS alert not received | Email not confirmed | Click confirmation link in the subscription email |
| File not moving to review folder | Wrong S3 permissions | Ensure `s3:PutObject` on the review prefix |
| Lambda not triggered | Wrong S3 prefix in notification | Confirm prefix filter is `submitted-invoices/` |

---

## 18. Security Best Practices

- **Never use root account** — create a least-privilege IAM user with only required permissions
- **Enable MFA** on both root and all IAM users
- **Scope S3 permissions** per prefix — Lambda only needs read on `submitted-invoices/` and write on `invoices-to-be-reviewed/`
- **Enable S3 server-side encryption** — SSE-S3 or SSE-KMS for invoice files at rest
- **Enable DynamoDB encryption** — on by default; consider Customer Managed Keys (CMK) for sensitive data
- **Enable CloudTrail** — logs all API calls for audit, debugging, and compliance
- **Rotate IAM credentials** — prefer short-lived role credentials over long-lived access keys
- **Set S3 lifecycle policies** — auto-delete reviewed files after N days to minimise storage costs

---

## 19. License

MIT License — see [LICENSE](LICENSE) for full terms.

---

**Built with:** Python 3.14 · AWS Lambda · Amazon Textract · Amazon DynamoDB · Amazon S3 · Amazon SNS
