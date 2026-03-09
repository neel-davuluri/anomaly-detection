#!/usr/bin/env python3
import json
import io
import logging
import boto3
import pandas as pd
from datetime import datetime
from baseline import BaselineManager
from detector import AnomalyDetector

logger = logging.getLogger(__name__)

s3 = boto3.client("s3")
NUMERIC_COLS = ["temperature", "humidity", "pressure", "wind_speed"]


def process_file(bucket: str, key: str):
    logger.info(f"Processing started: s3://{bucket}/{key}")
    print(f"Processing: s3://{bucket}/{key}")

    # 1. Download raw file
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        df = pd.read_csv(io.BytesIO(response["Body"].read()))
        logger.info(f"Loaded {len(df)} rows from {key}, columns: {list(df.columns)}")
        print(f"  Loaded {len(df)} rows, columns: {list(df.columns)}")
    except Exception as e:
        logger.error(f"Failed to download or parse file {key}: {e}")
        print(f"ERROR downloading {key}: {e}")
        return

    # 2. Load current baseline
    try:
        baseline_mgr = BaselineManager(bucket=bucket)
        baseline = baseline_mgr.load()
    except Exception as e:
        logger.error(f"Failed to initialize BaselineManager: {e}")
        print(f"ERROR loading baseline: {e}")
        return

    # 3. Update baseline with values from this batch BEFORE scoring
    for col in NUMERIC_COLS:
        if col in df.columns:
            try:
                clean_values = df[col].dropna().tolist()
                if clean_values:
                    baseline = baseline_mgr.update(baseline, col, clean_values)
            except Exception as e:
                logger.error(f"Failed to update baseline for column '{col}': {e}")
                print(f"ERROR updating baseline for {col}: {e}")
                continue

    # 4. Run detection
    try:
        detector = AnomalyDetector(z_threshold=3.0, contamination=0.05)
        scored_df = detector.run(df, NUMERIC_COLS, baseline, method="both")
    except Exception as e:
        logger.error(f"Detection failed for {key}: {e}")
        print(f"ERROR running detector on {key}: {e}")
        return

    # 5. Write scored file to processed/ prefix
    try:
        output_key = key.replace("raw/", "processed/")
        csv_buffer = io.StringIO()
        scored_df.to_csv(csv_buffer, index=False)
        s3.put_object(
            Bucket=bucket,
            Key=output_key,
            Body=csv_buffer.getvalue(),
            ContentType="text/csv"
        )
        logger.info(f"Scored file written to s3://{bucket}/{output_key}")
    except Exception as e:
        logger.error(f"Failed to write scored file {output_key}: {e}")
        print(f"ERROR writing scored CSV: {e}")
        return

    # 6. Save updated baseline back to S3, and sync the log file
    try:
        baseline_mgr.save(baseline, log_key="logs/anomaly-detection.log")
    except Exception as e:
        logger.error(f"Failed to save baseline after processing {key}: {e}")
        print(f"ERROR saving baseline: {e}")

    # 7. Build and write a processing summary
    try:
        anomaly_count = int(scored_df["anomaly"].sum()) if "anomaly" in scored_df else 0
        summary = {
            "source_key": key,
            "output_key": output_key,
            "processed_at": datetime.utcnow().isoformat(),
            "total_rows": len(df),
            "anomaly_count": anomaly_count,
            "anomaly_rate": round(anomaly_count / len(df), 4) if len(df) > 0 else 0,
            "baseline_observation_counts": {
                col: baseline.get(col, {}).get("count", 0) for col in NUMERIC_COLS
            }
        }

        summary_key = output_key.replace(".csv", "_summary.json")
        s3.put_object(
            Bucket=bucket,
            Key=summary_key,
            Body=json.dumps(summary, indent=2),
            ContentType="application/json"
        )
        logger.info(
            f"Processing complete for {key}: "
            f"{anomaly_count}/{len(df)} anomalies flagged. "
            f"Summary written to {summary_key}"
        )
        print(f"  Done: {anomaly_count}/{len(df)} anomalies flagged")
        return summary

    except Exception as e:
        logger.error(f"Failed to write summary for {key}: {e}")
        print(f"ERROR writing summary: {e}")
