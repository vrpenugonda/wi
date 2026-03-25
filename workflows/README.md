# WALLE GitHub Actions Workflows

This directory contains the GitHub Actions workflows for the WALLE classification pipeline. The pipeline has been broken down into modular components for better maintainability.

## Workflow Structure

### Active Workflows

The pipeline consists of the following workflow files:

1. **`walle-main.yaml`** (Main Orchestrator - ACTIVE)
   - Main orchestrator for the modular pipeline
   - Runs every 30 minutes or on manual trigger
   - Chains together all pipeline stages
   - Handles scheduling and manual dispatch
   - **Status**: Active production workflow

2. **`walle-prepare.yaml`** (Modular Component)
   - Data preparation and analysis stage
   - Fetches incidents from database
   - Determines worker parallelization strategy
   - Can be called by `walle-main.yaml` or run standalone

3. **`walle-l123.yaml`** (Modular Component)
   - L1/L2/L3 classification stage
   - Parallel worker-based classification
   - Merges results from all workers
   - Outputs: merged classified incidents + subcategory list

4. **`walle-l4.yaml`** (Modular Component)
   - L4 classification stage
   - Runs per-subcategory in parallel
   - Generates L4 taxonomies
   - Outputs: L4 checkpoints and taxonomy files

5. **`walle-finalize.yaml`** (Modular Component)
   - Pipeline finalization and reporting
   - Collects all artifacts
   - Sends failure notifications

## Legacy Workflow

- **`scheduled-pipeline.yaml.legacy`**: Original monolithic workflow (793 lines)
  - Archived but kept for reference
  - Do not enable - use `walle-main.yaml` instead

## Workflow Communication

The modular workflows communicate via:

- **Artifacts**: CSV files, JSON taxonomies uploaded/downloaded between stages
- **Workflow Outputs**: Metadata like timestamps, file paths, matrices passed between jobs
- **Workflow Call**: Using `workflow_call` trigger for reusable workflows

## Benefits of Modular Structure

1. **Maintainability**: Each workflow is ~200-300 lines instead of 800+
2. **Reusability**: Individual stages can be triggered independently
3. **Testing**: Easier to test individual components
4. **Debugging**: Clearer separation of concerns
5. **Parallel Development**: Multiple team members can work on different stages

## Running Workflows

### Run the Full Pipeline

```bash
# Via GitHub UI: Actions → WALLE Pipeline - Main Orchestrator → Run workflow
# Or wait for scheduled execution (every 30 minutes)
```

### Run Individual Stages (Advanced)

```bash
# Prepare data only
gh workflow run walle-prepare.yaml -f minutes_back=60

# Run L123 only (requires prepared data artifact)
gh workflow run walle-l123.yaml

# Run L4 only (requires L123 results)
gh workflow run walle-l4.yaml

# Finalize only
gh workflow run walle-finalize.yaml
```

## Secrets Required

All workflows require these secrets:

- `AZURE_ENDPOINT`, `AZURE_DEPLOYMENT`, `API_VERSION`
- `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID`, `TOKEN_URL`, `SCOPE`
- `PROJECT_ID`
- `DATAMARTDB`, `DATAMARTHOST`, `DATAMARTUSER`, `DATAMARTPASS`
- `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET_NAME`
- `ENTERPRISE_USERNAME`, `ENTERPRISE_TOKEN`

## Troubleshooting

### L123 Partition Upload Issues

If L123 partitions fail to upload:

- Check the "List output files for debugging" step in logs
- Verify files exist in `data/checkpoints/*.csv` and `data/output/*.csv`
- Pattern now broadened to: `l123-partition-*` (no timestamp requirement)

### L4 Artifact Upload Issues

If L4 artifacts fail to upload:

- Check "Debug: List L4 files before upload" step
- Files should be in `data/checkpoints/*_l4_checkpoint.csv`
- Taxonomies in `data/taxonomies/l4_*_taxonomy.json*`
- Multiple glob patterns added to catch naming variations

### Workflow Syntax Errors

- YAML does not allow inline comments in multi-line strings (`path: |`)
- Use separate comment blocks above the section
- Validate with: `python3 -c "import yaml; yaml.safe_load(open('workflow.yaml'))"`

## File Naming Conventions

### L123 Outputs

- Checkpoints: `data/checkpoints/partition_*_l123_checkpoint.csv`
- Merged: `data/output/classified_incidents_TIMESTAMP.csv`

### L4 Outputs

- Checkpoints: `data/checkpoints/l4_SUBCATEGORY_checkpoint.csv`
- Or: `data/checkpoints/classified_incidents_TIMESTAMP_SUBCATEGORY_l4_checkpoint.csv`
- Taxonomies: `data/taxonomies/l4_SUBCATEGORY_taxonomy.json`
- (Note: some files have double `.json.json` extension - patterns account for this)

## Contact

For questions or issues:

- William Farland: william_farland@optum.com
- Teja Marella: teja.marella@optum.com
- Team: walle_ops@optum.com
