# app.py
import io
import json
import logging
import os
import boto3
import pandas as pd
import requests
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from baseline import BaselineManager
from processor import process_file

# ── Logging setup ─────────────────────────────────────────────────────────────
log_file = "/var/log/anomaly-detection.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(),  # also print to stdout/screen
    ],
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Anomaly Detection Pipeline")

try:
    s3 = boto3.client("s3")
    BUCKET_NAME = os.environ["BUCKET_NAME"]
    logger.info(f"Application started. Using bucket: {BUCKET_NAME}")
except KeyError:
    logger.error("BUCKET_NAME environment variable is not set. Exiting.")
    raise RuntimeError("BUCKET_NAME environment variable is not set.")


# ── SNS subscription confirmation + message handler ──────────────────────────

@app.post("/notify")
async def handle_sns(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        msg_type = request.headers.get("x-amz-sns-message-type")
        logger.info(f"Received SNS message type: {msg_type}")

        # SNS sends a SubscriptionConfirmation before it will deliver any messages.
        if msg_type == "SubscriptionConfirmation":
            confirm_url = body.get("SubscribeURL")
            if not confirm_url:
                logger.error("SubscriptionConfirmation missing SubscribeURL")
                raise HTTPException(status_code=400, detail="Missing SubscribeURL")
            try:
                requests.get(confirm_url, timeout=10)
                logger.info(f"SNS subscription confirmed via: {confirm_url}")
            except requests.RequestException as e:
                logger.error(f"Failed to confirm SNS subscription: {e}")
                print(f"ERROR confirming SNS subscription: {e}")
                raise HTTPException(status_code=500, detail="Subscription confirmation failed")
            return {"status": "confirmed"}

        if msg_type == "Notification":
            try:
                s3_event = json.loads(body["Message"])
            except (KeyError, json.JSONDecodeError) as e:
                logger.error(f"Failed to parse SNS notification body: {e}")
                print(f"ERROR parsing SNS notification: {e}")
                raise HTTPException(status_code=400, detail="Invalid notification body")

            for record in s3_event.get("Records", []):
                try:
                    key = record["s3"]["object"]["key"]
                    if key.startswith("raw/") and key.endswith(".csv"):
                        logger.info(f"New file arrived: {key} — dispatching background task")
                        background_tasks.add_task(process_file, BUCKET_NAME, key)
                except KeyError as e:
                    logger.error(f"Malformed S3 record in SNS event: {e}")
                    print(f"ERROR reading S3 record: {e}")

        return {"status": "ok"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in /notify: {e}")
        print(f"ERROR in /notify: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ── Query endpoints ───────────────────────────────────────────────────────────

@app.get("/anomalies/recent")
def get_recent_anomalies(limit: int = 50):
    """Return rows flagged as anomalies across the 10 most recent processed files."""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix="processed/")

        keys = sorted(
            [
                obj["Key"]
                for page in pages
                for obj in page.get("Contents", [])
                if obj["Key"].endswith(".csv")
            ],
            reverse=True,
        )[:10]

        all_anomalies = []
        for key in keys:
            try:
                response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
                df = pd.read_csv(io.BytesIO(response["Body"].read()))
                if "anomaly" in df.columns:
                    flagged = df[df["anomaly"] == True].copy()
                    flagged["source_file"] = key
                    all_anomalies.append(flagged)
            except Exception as e:
                logger.error(f"Failed to read processed file {key}: {e}")
                print(f"ERROR reading {key}: {e}")
                continue

        if not all_anomalies:
            return {"count": 0, "anomalies": []}

        combined = pd.concat(all_anomalies).head(limit)
        logger.info(f"/anomalies/recent returned {len(combined)} rows")
        return {"count": len(combined), "anomalies": combined.to_dict(orient="records")}

    except Exception as e:
        logger.error(f"Error in /anomalies/recent: {e}")
        print(f"ERROR in /anomalies/recent: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve recent anomalies")


@app.get("/anomalies/summary")
def get_anomaly_summary():
    """Aggregate anomaly rates across all processed files using their summary JSONs."""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix="processed/")

        summaries = []
        for page in pages:
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("_summary.json"):
                    try:
                        response = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
                        summaries.append(json.loads(response["Body"].read()))
                    except Exception as e:
                        logger.error(f"Failed to read summary file {obj['Key']}: {e}")
                        print(f"ERROR reading summary {obj['Key']}: {e}")
                        continue

        if not summaries:
            return {"message": "No processed files yet."}

        total_rows = sum(s["total_rows"] for s in summaries)
        total_anomalies = sum(s["anomaly_count"] for s in summaries)

        logger.info(f"/anomalies/summary: {len(summaries)} files, {total_anomalies}/{total_rows} anomalies")
        return {
            "files_processed": len(summaries),
            "total_rows_scored": total_rows,
            "total_anomalies": total_anomalies,
            "overall_anomaly_rate": round(total_anomalies / total_rows, 4) if total_rows > 0 else 0,
            "most_recent": sorted(summaries, key=lambda x: x["processed_at"], reverse=True)[:5],
        }

    except Exception as e:
        logger.error(f"Error in /anomalies/summary: {e}")
        print(f"ERROR in /anomalies/summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve anomaly summary")


@app.get("/baseline/current")
def get_current_baseline():
    """Show the current per-channel statistics the detector is working from."""
    try:
        baseline_mgr = BaselineManager(bucket=BUCKET_NAME)
        baseline = baseline_mgr.load()

        channels = {}
        for channel, stats in baseline.items():
            if channel == "last_updated":
                continue
            try:
                channels[channel] = {
                    "observations": stats["count"],
                    "mean": round(stats["mean"], 4),
                    "std": round(stats.get("std", 0.0), 4),
                    "baseline_mature": stats["count"] >= 30,
                }
            except (KeyError, TypeError) as e:
                logger.error(f"Malformed baseline entry for channel {channel}: {e}")
                print(f"ERROR reading baseline channel {channel}: {e}")
                continue

        logger.info(f"/baseline/current: {len(channels)} channels loaded")
        return {
            "last_updated": baseline.get("last_updated"),
            "channels": channels,
        }

    except Exception as e:
        logger.error(f"Error in /baseline/current: {e}")
        print(f"ERROR in /baseline/current: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve baseline")


@app.get("/health")
def health():
    return {"status": "ok", "bucket": BUCKET_NAME, "timestamp": datetime.utcnow().isoformat()}
