"""
Data loading and saving utilities
"""

import csv
import json
from pathlib import Path
from typing import List, Dict, Any, Optional, Literal
from datetime import datetime
import pandas as pd

from ..config import get_settings


class DataLoader:
    """Handles loading and saving incident data"""
    
    def __init__(self, settings=None):
        self.settings = settings or get_settings()
    
    def load_csv(self, filepath: str | Path) -> Optional[pd.DataFrame]:
        """Load incidents from CSV file. Returns None if file doesn't exist."""
        filepath = Path(filepath)
        if not filepath.is_absolute():
            filepath = self.settings.input_dir / filepath
        
        if not filepath.exists():
            return None
        
        print(f"Loading data from {filepath}...")
        try:
            df = pd.read_csv(filepath)
        except pd.errors.ParserError:
            # Handle malformed CSV with inconsistent column counts
            print(f"   Warning: CSV has inconsistent columns, using error recovery mode")
            df = pd.read_csv(filepath, on_bad_lines='skip')
        print(f"   Loaded {len(df):,} records")
        return df
    
    def save_csv(
        self,
        df: pd.DataFrame,
        filepath: str | Path,
        mode: Literal['w', 'a'] = 'w'
    ):
        """Save DataFrame to CSV"""
        filepath = Path(filepath)
        if not filepath.is_absolute():
            filepath = self.settings.output_dir / filepath
        
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        df.to_csv(filepath, mode=mode, index=False, header=(mode == 'w'))
        print(f"💾 Saved {len(df):,} records to {filepath}")
    
    def append_results(
        self,
        filepath: str | Path,
        results: List[Dict[str, Any]],
    ):
        """Append classification results to CSV"""
        if not results:
            return
        
        # Filter out empty dicts and dicts without incident_id
        valid_results = [r for r in results if r and r.get('incident_id')]
        if not valid_results:
            return
        
        filepath = Path(filepath)
        if not filepath.is_absolute():
            filepath = self.settings.output_dir / filepath
        
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        file_exists = filepath.exists()
        
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(
                f, 
                fieldnames=valid_results[0].keys(),
                quoting=csv.QUOTE_ALL,  # Quote all fields to handle commas/newlines
                extrasaction='ignore',
            )
            if not file_exists:
                writer.writeheader()
            writer.writerows(valid_results)
    
    def load_taxonomy(self, name: str) -> Optional[Dict]:
        """Load L4 taxonomy from JSON file"""
        filepath = Path(name)
        
        if not filepath.is_absolute():
            filepath = self.settings.taxonomy_dir / f"taxonomy_{name}.json"
        
        if not filepath.exists():
            return None
        
        with open(filepath, 'r') as f:
            return json.load(f)
    
    def save_taxonomy(self, name: str, taxonomy: Dict):
        """Save L4 taxonomy to JSON file"""
        filepath = self.settings.taxonomy_dir / f"taxonomy_{name}.json"
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, 'w') as f:
            json.dump(taxonomy, f, indent=2, default=str)
        
        print(f"Saved taxonomy to {filepath}")
    
    def load_checkpoint(self, name: str) -> Optional[pd.DataFrame]:
        """Load checkpoint file"""
        filepath = Path(name)
        
        # If it's already an absolute path or includes .csv, use directly
        if not filepath.is_absolute() and not str(name).endswith('.csv'):
            filepath = self.settings.checkpoint_dir / f"{name}_checkpoint.csv"
        elif not filepath.is_absolute():
            filepath = Path(name)
        
        if not filepath.exists():
            return None
        
        try:
            return pd.read_csv(filepath)
        except pd.errors.ParserError:
            # Handle malformed CSV with inconsistent column counts
            return pd.read_csv(filepath, on_bad_lines='skip')
    
    def save_checkpoint(self, name: str, df: pd.DataFrame):
        """Save checkpoint file"""
        filepath = self.settings.checkpoint_dir / f"{name}_checkpoint.csv"
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        df.to_csv(filepath, index=False)
        print(f"💾 Checkpoint saved: {filepath}")
    
    def get_pending_records(
        self,
        input_df: pd.DataFrame,
        checkpoint_df: Optional[pd.DataFrame],
        id_column: str = 'in_id'
    ) -> pd.DataFrame:
        """Get records that haven't been processed yet"""
        if checkpoint_df is None or len(checkpoint_df) == 0:
            return input_df
        
        # Find the actual ID column in each dataframe (handle in_id vs incident_id)
        id_candidates = ['in_id', 'incident_id', 'Incident ID']
        
        input_id_col = id_column
        if input_id_col not in input_df.columns:
            for c in id_candidates:
                if c in input_df.columns:
                    input_id_col = c
                    break
        
        checkpoint_id_col = id_column
        if checkpoint_id_col not in checkpoint_df.columns:
            for c in id_candidates:
                if c in checkpoint_df.columns:
                    checkpoint_id_col = c
                    break
        
        if input_id_col not in input_df.columns:
            print(f"   Warning: ID column '{id_column}' not found in input. Columns: {list(input_df.columns)[:10]}")
            return input_df
        
        if checkpoint_id_col not in checkpoint_df.columns:
            print(f"   Warning: ID column not found in checkpoint. Processing all records.")
            return input_df
        
        processed_ids = set(checkpoint_df[checkpoint_id_col].astype(str))
        pending = input_df[~input_df[input_id_col].astype(str).isin(processed_ids)]
        
        print(f"   Total: {len(input_df):,}, Processed: {len(checkpoint_df):,}, Pending: {len(pending):,}")
        
        return pending
    
    def generate_run_filename(self, prefix: str = "classification") -> str:
        """Generate a timestamped filename for a run"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{prefix}_{timestamp}.csv"
