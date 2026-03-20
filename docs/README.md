# WALLE Insights

LLM-powered incident classification and analysis system.

## Features

- Two-step classification pipeline (L1/L2/L3 + L4)
- Automated incident categorization using AI
- Integration with ServiceNow data sources
- S3 and Snowflake output support

## Installation

```bash
uv pip install -e .
```

## Usage

```bash
# Run classification pipeline
python -m insights run --start-date 2025-07-01

# Or use the CLI
walle run --start-date 2025-07-01
```
