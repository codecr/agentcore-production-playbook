# Acme Lending — Mortgage Eligibility Policy v1.0

**Document type:** Policy (formal rules)
**Effective date:** May 2026
**Owner:** Acme Lending Risk & Compliance
**Purpose:** Defines eligibility rules for mortgage products offered by Acme Lending. This document is the source of truth for the customer support agent's responses about mortgage eligibility. All rules in this document are formal and must be applied without exception.

---

## 1. Definitions

- **Applicant**: A natural person applying for a mortgage product.
- **Credit score**: The applicant's FICO score, between 300 and 850.
- **Income**: The applicant's verified annual gross income in US dollars.
- **DTI (Debt-to-Income)**: The applicant's monthly debt obligations divided by monthly gross income, expressed as a percentage.
- **Loan amount**: The principal amount of the requested mortgage in US dollars.
- **Down payment percentage**: The down payment divided by the property value, expressed as a percentage.
- **Employment status**: One of {EMPLOYED_FULL_TIME, EMPLOYED_PART_TIME, SELF_EMPLOYED, RETIRED, UNEMPLOYED}.
- **Property type**: One of {SINGLE_FAMILY, CONDO, MULTI_FAMILY, INVESTMENT}.

---

## 2. Product tiers

Acme Lending offers three mortgage product tiers, each with distinct eligibility requirements.

### 2.1 Premium tier

The Premium tier offers the lowest interest rates and is reserved for applicants meeting all of the following requirements:

1. Credit score must be at least 740.
2. DTI must be less than or equal to 36%.
3. Down payment percentage must be at least 20%.
4. Employment status must be EMPLOYED_FULL_TIME or SELF_EMPLOYED with at least 2 years of verified income.
5. Loan amount must not exceed $1,500,000.

### 2.2 Standard tier

The Standard tier is available to applicants meeting all of the following requirements:

1. Credit score must be at least 680 and less than 740.
2. DTI must be less than or equal to 43%.
3. Down payment percentage must be at least 10%.
4. Employment status must be EMPLOYED_FULL_TIME, EMPLOYED_PART_TIME, or SELF_EMPLOYED.
5. Loan amount must not exceed $750,000.

### 2.3 Basic tier

The Basic tier is available to applicants meeting all of the following requirements:

1. Credit score must be at least 620 and less than 680.
2. DTI must be less than or equal to 50%.
3. Down payment percentage must be at least 5%.
4. Employment status must not be UNEMPLOYED.
5. Loan amount must not exceed $400,000.

---

## 3. Documentation requirements

Documentation requirements vary by loan amount.

### 3.1 Standard documentation

Loans of $40,000 or less require standard documentation:

- Photo ID
- Two most recent pay stubs (or two most recent tax returns for self-employed applicants)
- Three months of bank statements

### 3.2 Enhanced documentation

Loans of more than $40,000 and up to $250,000 require enhanced documentation:

- All standard documentation requirements
- Two years of tax returns
- Employment verification letter
- Asset documentation for the down payment source

### 3.3 Comprehensive documentation

Loans exceeding $250,000 require comprehensive documentation:

- All enhanced documentation requirements
- Three years of tax returns
- Audited financial statements (for self-employed applicants)
- Property appraisal from an Acme-approved appraiser
- Title insurance commitment

---

## 4. Disqualifying conditions

Regardless of tier, an applicant is disqualified if any of the following conditions apply:

1. The applicant has had a bankruptcy discharge within the last 4 years.
2. The applicant has had a mortgage foreclosure within the last 7 years.
3. The applicant has unpaid federal tax liens.
4. The applicant has been convicted of mortgage fraud at any time.

---

## 5. Investment property restrictions

For property type INVESTMENT, the following additional restrictions apply:

1. The Basic tier is not available.
2. Down payment percentage must be at least 25% regardless of tier.
3. Maximum loan amount is reduced by 20% from the tier's standard maximum.

---

## 6. Co-applicant rules

When the application includes a co-applicant:

1. The lower of the two credit scores is used for tier determination.
2. Combined verified income is used for DTI calculation.
3. Both applicants must independently satisfy the disqualifying conditions check in Section 4.

---

*End of Acme Lending Mortgage Eligibility Policy v1.0.*

*This document is structured for upload to Amazon Bedrock Automated Reasoning Checks. Each rule is expressed in formal language: specific numeric thresholds, specific enumerated values, explicit conditional structure. Avoid editing this document with subjective language ("reasonable judgment," "as appropriate," etc.) — those edits will reduce the policy's verifiability.*
