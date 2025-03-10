import os
import json
import io
import threading
import time
from datetime import datetime

import boto3
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'your-secret-key'  # Replace with a secure key

# ---------------------------------------------------------------------
# 1) Configuration
# ---------------------------------------------------------------------
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'logstore')

# Single "global" endpoint behind HAProxy
GLOBAL_ENDPOINT = os.environ.get('GLOBAL_ENDPOINT', 'https://s3.eu.cephlabs.com')

AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', 'test')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', 'test')

MY_DC = os.environ.get('MY_DC', 'Madrid')
LOCAL_DC = os.environ.get('LOCAL_DC', MY_DC)
bucket_quota_mb = float(os.environ.get('BUCKET_QUOTA_MB', 30))

# Additional environment variables (if you use them in the infrastructure route)
DC1_LB_ENDPOINT = os.environ.get('DC1_LB_ENDPOINT', 'https://s3.mad.eu.cephlabs.com')
DC2_LB_ENDPOINT = os.environ.get('DC2_LB_ENDPOINT', 'https://s3.par.eu.cephlabs.com')

RGW_DC1_ENDPOINTS_ENV = os.environ.get('RGW_DC1_ENDPOINTS', 'http://ceph-node-00:8088, http://ceph-node-01:8088')
RGW_DC1_ENDPOINTS_LIST = [ep.strip() for ep in RGW_DC1_ENDPOINTS_ENV.split(',') if ep.strip()]
RGW_DC2_ENDPOINTS_ENV = os.environ.get('RGW_DC2_ENDPOINTS', 'http://ceph-node-05:8088, http://ceph-node-06:8088')
RGW_DC2_ENDPOINTS_LIST = [ep.strip() for ep in RGW_DC2_ENDPOINTS_ENV.split(',') if ep.strip()]

# ---------------------------------------------------------------------
# 2) S3 Client (Global)
# ---------------------------------------------------------------------
s3 = boto3.client(
    's3',
    endpoint_url=GLOBAL_ENDPOINT,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

# ---------------------------------------------------------------------
# 3) Global Data + S3 Persistence for "upload_origins"
# ---------------------------------------------------------------------
replication_statuses = {}

# We'll store "upload_origins" in a single JSON object in the bucket.
UPLOAD_ORIGINS_KEY = "_internal_data/upload_origins.json"
upload_origins = {}  # key => "Madrid" / "Paris" / etc.

def merge_origins_dict(local_dict, remote_dict):
    """Merge remote_dict into local_dict, never overwriting a known zone with 'Unknown'."""
    for k, v_remote in remote_dict.items():
        v_local = local_dict.get(k)
        if v_local is None or v_local == "Unknown":
            local_dict[k] = v_remote
    return local_dict

def load_upload_origins_from_s3():
    global upload_origins
    try:
        resp = s3.get_object(Bucket=BUCKET_NAME, Key=UPLOAD_ORIGINS_KEY)
        data = resp['Body'].read()
        remote_dict = json.loads(data.decode('utf-8'))
        upload_origins = merge_origins_dict(upload_origins, remote_dict)
        print(f"[INFO] Loaded upload_origins from S3 ({len(upload_origins)} entries).")
    except s3.exceptions.NoSuchKey:
        print("[INFO] upload_origins.json not found in S3; starting empty.")
        upload_origins = {}
    except Exception as e:
        print(f"[WARN] Could not load upload_origins from S3: {e}")
        upload_origins = {}

def save_upload_origins_to_s3():
    global upload_origins
    try:
        # Merge remote data first
        try:
            resp = s3.get_object(Bucket=BUCKET_NAME, Key=UPLOAD_ORIGINS_KEY)
            remote_dict = json.loads(resp['Body'].read().decode('utf-8'))
            upload_origins = merge_origins_dict(upload_origins, remote_dict)
        except s3.exceptions.NoSuchKey:
            pass
        payload = json.dumps(upload_origins).encode('utf-8')
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=UPLOAD_ORIGINS_KEY,
            Body=payload,
            ContentType='application/json'
        )
        print(f"[INFO] Saved upload_origins to S3 ({len(upload_origins)} entries).")
    except Exception as e:
        print(f"[WARN] Could not save upload_origins to S3: {e}")

# ---------------------------------------------------------------------
# 4) Replication Check (Unchanged)
# ---------------------------------------------------------------------
def get_object_replication_status_with_header(remote_rgw_base_url, object_key):
    remote_url = f"{remote_rgw_base_url}/{BUCKET_NAME}/{object_key}"
    try:
        resp = requests.head(remote_url, timeout=2)
        if resp.status_code != 200:
            return "NO"
        rep_status = resp.headers.get("x-amz-replication-status", "").strip()
        if rep_status not in ("REPLICA", "COMPLETED"):
            return "NO"
        if "x-rgw-replicated-from" not in resp.headers:
            return "NO"
        return "YES"
    except Exception:
        return "NO"

def check_replication_status_for_object(object_key):
    endpoints = RGW_DC1_ENDPOINTS_LIST + RGW_DC2_ENDPOINTS_LIST
    for ep in endpoints:
        status = get_object_replication_status_with_header(ep, object_key)
        if status == "YES":
            return "YES"
    return "NO"

def background_replication_check():
    while True:
        try:
            resp = s3.list_objects_v2(Bucket=BUCKET_NAME)
            contents = resp.get('Contents', [])
            for obj in contents:
                key = obj['Key']
                if replication_statuses.get(key) == "YES":
                    continue
                status = check_replication_status_for_object(key)
                replication_statuses[key] = status
            time.sleep(30)
        except Exception as e:
            print("[WARN] Replication check error:", e)
            time.sleep(25)

threading.Thread(target=background_replication_check, daemon=True).start()

# ---------------------------------------------------------------------
# 5) Flask Routes
# ---------------------------------------------------------------------
@app.route('/preview/<path:key>')
def preview(key):
    try:
        obj = s3.get_object(Bucket=BUCKET_NAME, Key=key)
        content_type = obj.get('ContentType', '')
        key_lower = key.lower()
        if content_type.startswith('text') or key_lower.endswith('.txt') or key_lower.endswith('.log'):
            content = obj['Body'].read().decode('utf-8', errors='replace')
            return render_template('preview_text.html', key=key, content=content,
                                   rgw_endpoint=GLOBAL_ENDPOINT, bucket_name=BUCKET_NAME)
        elif key_lower.endswith('.png') or key_lower.endswith('.jpg') or key_lower.endswith('.jpeg'):
            file_url = f"{GLOBAL_ENDPOINT}/{BUCKET_NAME}/{key}"
            return render_template('preview_image.html', key=key, file_url=file_url)
        else:
            return "Preview not available for this file type.", 400
    except Exception as e:
        return f"Error previewing file: {e}", 500

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part', 'warning')
            return redirect(request.url)

        upfile = request.files['file']
        if upfile.filename == '':
            flash('No file selected', 'warning')
            return redirect(request.url)

        if upfile:
            file_bytes = upfile.read()
            s3_filename = datetime.utcnow().strftime('%Y%m%d%H%M%S_') + secure_filename(upfile.filename)

            try:
                put_resp = s3.put_object(
                    Bucket=BUCKET_NAME,
                    Key=s3_filename,
                    Body=file_bytes,
                    ACL='public-read'
                )
                headers = put_resp['ResponseMetadata'].get('HTTPHeaders', {})
                actual_zone = headers.get('x-served-by', 'Unknown')

                # Update the dictionary if we have a known zone (not Unknown)
                if actual_zone != "Unknown":
                    upload_origins[s3_filename] = actual_zone
                # Otherwise, if not set, do not overwrite a known value
                save_upload_origins_to_s3()

                flash(f"File uploaded successfully! Served by {actual_zone}.", "success")

            except Exception as e:
                flash(f"Upload failed: {e}", "danger")

            return redirect(url_for('index'))
    
    # GET: List objects
    objects = []
    try:
        # Load (and merge) upload_origins from S3 to get the latest known data
        load_upload_origins_from_s3()

        resp = s3.list_objects_v2(Bucket=BUCKET_NAME)
        contents = resp.get('Contents', [])
        for obj in contents:
            key = obj['Key']
            if key.startswith("_internal_data/"):
                continue

            obj['replication_status'] = replication_statuses.get(key, "Pending")
            obj['upload_origin'] = upload_origins.get(key, "Unknown")
            objects.append(obj)
    except Exception as e:
        flash(f"Error listing objects: {e}", "danger")

    # Compute upload_counts by zone based on the upload_origin from our dictionary
    upload_counts = {}
    for obj in objects:
        zone = obj.get('upload_origin', 'Unknown')
        upload_counts[zone] = upload_counts.get(zone, 0) + 1

    total_usage = sum(o['Size'] for o in objects) if objects else 0
    total_usage_mb = total_usage / (1024 * 1024)
    usage_percentage = (total_usage_mb / bucket_quota_mb) * 100 if bucket_quota_mb else 0
    if usage_percentage > 100:
        usage_percentage = 100

    return render_template(
        'index.html',
        objects=objects,
        rgw_endpoint=GLOBAL_ENDPOINT,
        bucket_name=BUCKET_NAME,
        upload_counts=upload_counts,
        my_dc=MY_DC,
        bucket_usage=total_usage_mb,
        bucket_quota=bucket_quota_mb,
        usage_percentage=usage_percentage
    )

@app.route('/api/filelist', methods=['GET'])
def api_filelist():
    try:
        load_upload_origins_from_s3()
        resp = s3.list_objects_v2(Bucket=BUCKET_NAME)
        contents = resp.get('Contents', [])
        objects = []
        for obj in contents:
            key = obj['Key']
            if key.startswith("_internal_data/"):
                continue
            obj['replication_status'] = replication_statuses.get(key, "Pending")
            obj['upload_origin'] = upload_origins.get(key, "Unknown")
            objects.append(obj)
        return jsonify(objects)
    except Exception as e:
        return jsonify([])

@app.route('/healthz', methods=['GET'])
def healthz():
    try:
        return jsonify({"status": "OK"}), 200
    except Exception as e:
        return jsonify({"status": "ERROR", "message": str(e)}), 500

@app.route('/infrastructure', methods=['GET'])
def infrastructure():
    return render_template("infrastructure.html")

@app.route('/api/infrastructure', methods=['GET'])
def api_infrastructure():
    endpoints = [
        {"name": "Global Endpoint", "url": GLOBAL_ENDPOINT},
        {"name": "LB - Madrid", "url": DC1_LB_ENDPOINT},
        {"name": "LB - Paris", "url": DC2_LB_ENDPOINT}
    ]
    health_results = {}
    for ep in endpoints:
        try:
            r = requests.get(ep["url"], timeout=2)
            status = "Up" if r.status_code == 200 else "Down"
        except Exception:
            status = "Down"
        health_results[ep["name"]] = {"url": ep["url"], "status": status}
    
    rgw_list_madrid = []
    for idx, ep in enumerate(RGW_DC1_ENDPOINTS_LIST, start=1):
        try:
            r = requests.get(ep, timeout=2)
            stat = "Up" if r.status_code == 200 else "Down"
        except Exception:
            stat = "Down"
        rgw_list_madrid.append({"name": f"RGW Madrid - {idx}", "url": ep, "status": stat})

    rgw_list_paris = []
    for idx, ep in enumerate(RGW_DC2_ENDPOINTS_LIST, start=1):
        try:
            r = requests.get(ep, timeout=2)
            stat = "Up" if r.status_code == 200 else "Down"
        except Exception:
            stat = "Down"
        rgw_list_paris.append({"name": f"RGW Paris - {idx}", "url": ep, "status": stat})

    remote_status_vals = list(replication_statuses.values())
    all_yes = (remote_status_vals and all(s == "YES" for s in remote_status_vals))
    average_replication = "YES" if all_yes else "NO"

    data = {
        "global_result": health_results.get("Global Endpoint", {"status": "Down"}),
        "lb_dc1": health_results.get("LB - Madrid", {"status": "Down"}),
        "lb_dc2": health_results.get("LB - Paris", {"status": "Down"}),
        "rgw_dc1": rgw_list_madrid,
        "rgw_dc2": rgw_list_paris,
        "average_replication": average_replication,
        "timestamp": datetime.utcnow().isoformat()
    }
    return jsonify(data)

# ---------------------------------------------------------------------
# 6) App Startup
# ---------------------------------------------------------------------
if __name__ == '__main__':
    load_upload_origins_from_s3()
    app.run(host='0.0.0.0', port=5000, debug=True)

