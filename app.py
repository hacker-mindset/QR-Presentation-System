from flask import Flask, render_template, request, jsonify, send_file
import os
import sys
import secrets
import subprocess
import shutil
from pathlib import Path
import socket
import io
import fitz
import qrcode
import base64
import atexit
import webbrowser
import threading
import re
import time
from datetime import datetime

BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static")
)

# =========================
# SETTINGS
# =========================

PORT = 5000
PUBLIC_BASE_URL = os.environ.get("QR_PUBLIC_BASE_URL", "").strip().rstrip("/")
# ============================================================
# AUTOMATIC CLOUDFLARE QUICK TUNNEL
# ============================================================

cloudflare_process = None


def start_cloudflare_tunnel():
    """
    Automatically start Cloudflare Quick Tunnel.
    The generated public URL is stored in PUBLIC_BASE_URL.
    """

    global cloudflare_process
    global PUBLIC_BASE_URL

    # If a public URL was manually provided, use it.
    if PUBLIC_BASE_URL:
        print("Using configured public URL:", PUBLIC_BASE_URL)
        return

    # Find cloudflared.exe
    cloudflared_path = os.path.join(BASE_DIR, "cloudflared.exe")

    if not os.path.exists(cloudflared_path):
        print("cloudflared.exe not found.")
        print("Local network mode will be used.")
        return

    print("Starting Cloudflare Quick Tunnel...")

    try:
        cloudflare_process = subprocess.Popen(
            [
                cloudflared_path,
                "tunnel",
                "--protocol", "http2",
                "--url", f"http://127.0.0.1:{PORT}"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        start_time = time.time()

        while time.time() - start_time < 30:

            line = cloudflare_process.stdout.readline()

            if not line:
                time.sleep(0.2)
                continue

            line = line.strip()

            print("[Cloudflare]", line)

            match = re.search(
                r"https://[a-zA-Z0-9-]+\.trycloudflare\.com",
                line
            )

            if match:
                PUBLIC_BASE_URL = match.group(0).rstrip("/")

                print("")
                print("==========================================")
                print("PUBLIC URL:")
                print(PUBLIC_BASE_URL)
                print("==========================================")
                print("")

                return

        print("Cloudflare public URL was not detected.")
        print("Local network mode will be used.")

    except Exception as e:
        print("Cloudflare startup error:", e)
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

ALLOWED_EXTENSIONS = {".pdf", ".ppt", ".pptx"}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =========================
# TEMPORARY SESSION
# =========================

current_session = None


# =========================
# GET LOCAL IP
# =========================

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


# =========================
# CREATE NEW SESSION
# =========================

def create_new_session():

    global current_session

    current_session = {
        "token": secrets.token_urlsafe(16),
        "student_name": "",
        "filename": "",
        "filepath": "",
        "total_pages": 0,
        "current_page": 1,
        "connected": False,
        "uploading": False,
        "upload_progress": 0,
        "created_at": datetime.now().isoformat()
    }

    print()
    print("======================================")
    print(" NEW PRESENTATION SESSION CREATED")
    print("======================================")
    print("Token:", current_session["token"])
    print()


# =========================
# DELETE TEMP FILE
# =========================

def cleanup_session():

    global current_session

    if current_session:

        # Delete converted PDF and original PPT/PPTX
        files_to_delete = [
            current_session.get("filepath", ""),
            current_session.get("original_filepath", "")
        ]

        # Remove duplicate paths
        files_to_delete = list(dict.fromkeys(
            path for path in files_to_delete if path
        ))

        for filepath in files_to_delete:

            if os.path.exists(filepath):

                try:
                    os.remove(filepath)
                    print("Temporary file deleted:", filepath)

                except Exception as e:
                    print("File delete error:", e)

    current_session = None


# =========================
# ALLOWED FILE
# =========================


def find_libreoffice():
    """
    Find LibreOffice on Windows.
    Checks PATH and common installation locations.
    """

    for command in ["soffice.com", "soffice.exe", "soffice"]:
        found = shutil.which(command)
        if found:
            return Path(found)

    common_paths = [
        Path(r"C:\Program Files\LibreOffice\program\soffice.com"),
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.com"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ]

    for path in common_paths:
        if path.exists():
            return path

    return None


def convert_presentation_to_pdf(input_file):
    """
    Convert PPT/PPTX to PDF using LibreOffice.
    PDF files do not use this function.
    """

    input_path = Path(input_file)
    output_dir = input_path.parent

    soffice = find_libreoffice()

    if not soffice:
        raise RuntimeError(
            "PPT/PPTX files require LibreOffice. "
            "LibreOffice was not found on this computer. "
            "PDF files work without LibreOffice."
        )

    print("LibreOffice found:", soffice)

    result = subprocess.run(
        [
            str(soffice),
            "--headless",
            "--convert-to", "pdf",
            "--outdir", str(output_dir),
            str(input_path)
        ],
        capture_output=True,
        text=True,
        timeout=120
    )

    pdf_path = output_dir / (input_path.stem + ".pdf")

    if not pdf_path.exists():
        error = result.stderr.strip() or result.stdout.strip()

        raise RuntimeError(
            "PPT/PPTX to PDF conversion failed. " + error
        )

    print("Presentation converted to PDF:", pdf_path)

    return str(pdf_path)

def allowed_file(filename):

    ext = os.path.splitext(filename)[1].lower()

    return ext in ALLOWED_EXTENSIONS


# =========================
# QR CODE
# =========================

def generate_qr():

    token = current_session["token"]

    if PUBLIC_BASE_URL:
        student_url = f"{PUBLIC_BASE_URL}/student/{token}"
    else:
        student_url = f"http://{get_local_ip()}:{PORT}/student/{token}"

    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=4
    )

    qr.add_data(student_url)
    qr.make(fit=True)

    img = qr.make_image()

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")

    buffer.seek(0)

    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")

    return encoded, student_url


# =========================
# MAIN SCREEN
# =========================

@app.route("/")
def main_screen():

    qr_image, student_url = generate_qr()

    return render_template(
        "main.html",
        session=current_session,
        qr_image=qr_image,
        student_url=student_url
    )


# =========================
# STUDENT SCREEN
# =========================

@app.route("/student/<token>")
def student_screen(token):

    if not current_session:
        return "Session expired. Please scan the new QR code.", 404

    if token != current_session["token"]:
        return "Invalid or expired QR code.", 404

    return render_template(
        "student.html",
        session=current_session
    )


# =========================
# UPLOAD PRESENTATION
# =========================

@app.route("/student/<token>/upload", methods=["POST"])
def upload_presentation(token):

    global current_session

    if not current_session:
        return jsonify({
            "success": False,
            "message": "Session expired."
        }), 400

    if token != current_session["token"]:
        return jsonify({
            "success": True,
            "message": "This presentation session has already ended."
        })

    # Upload status
    current_session["uploading"] = True
    current_session["upload_progress"] = 0

    student_name = request.form.get("student_name", "").strip()

    if not student_name:
        return jsonify({
            "success": False,
            "message": "Please enter your name."
        }), 400

    if "presentation" not in request.files:
        return jsonify({
            "success": False,
            "message": "Please select a PDF, PPT or PPTX file."
        }), 400

    file = request.files["presentation"]

    if not file.filename:
        return jsonify({
            "success": False,
            "message": "No file selected."
        }), 400

    if not allowed_file(file.filename):
        return jsonify({
            "success": False,
            "message": "Only PDF, PPT and PPTX files are supported."
        }), 400

    file_data = file.read()

    if len(file_data) > MAX_FILE_SIZE:
        return jsonify({
            "success": False,
            "message": "File is too large. Maximum size is 50 MB."
        }), 400

    # Remove previous temporary files
    for old_path in [
        current_session.get("filepath", ""),
        current_session.get("original_filepath", "")
    ]:
        if old_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except Exception:
                pass

    original_ext = Path(file.filename).suffix.lower()

    original_filepath = os.path.join(
        UPLOAD_FOLDER,
        f"{token}{original_ext}"
    )

    filepath = original_filepath

    try:

        # Save uploaded file temporarily
        with open(original_filepath, "wb") as f:
            f.write(file_data)

        # Convert PPT/PPTX to PDF
        if original_ext in {".ppt", ".pptx"}:
            filepath = convert_presentation_to_pdf(original_filepath)

        # Open converted/original PDF
        pdf = fitz.open(filepath)

        total_pages = pdf.page_count

        pdf.close()

        if total_pages < 1:

            for cleanup_path in [filepath, original_filepath]:
                if cleanup_path and os.path.exists(cleanup_path):
                    try:
                        os.remove(cleanup_path)
                    except Exception:
                        pass

            return jsonify({
                "success": False,
                "message": "Presentation has no pages."
            }), 400

        current_session["student_name"] = student_name
        current_session["filename"] = file.filename
        current_session["filepath"] = filepath
        current_session["original_filepath"] = original_filepath
        current_session["total_pages"] = total_pages
        current_session["current_page"] = 1
        current_session["connected"] = True

        print()
        print("======================================")
        print(" STUDENT CONNECTED")
        print("======================================")
        print("Student :", student_name)
        print("File    :", file.filename)
        print("Type    :", original_ext.upper())
        print("Pages   :", total_pages)
        print("======================================")
        print()

        return jsonify({
            "success": True,
            "message": "Presentation uploaded successfully.",
            "redirect": f"/student/{token}"
        })

    except Exception as e:

        for cleanup_path in [filepath, original_filepath]:
            if cleanup_path and os.path.exists(cleanup_path):
                try:
                    os.remove(cleanup_path)
                except Exception:
                    pass

        return jsonify({
            "success": False,
            "message": f"Could not process presentation: {str(e)}"
        }), 500


# =========================
# CHUNK UPLOAD
# =========================

@app.route("/student/<token>/upload-chunk", methods=["POST"])
def upload_chunk(token):

    global current_session

    if not current_session:
        return jsonify({
            "success": False,
            "message": "Session expired."
        }), 400

    if token != current_session["token"]:
        return jsonify({
            "success": False,
            "message": "This presentation session has already ended."
        }), 400

    student_name = request.headers.get("X-Student-Name", "").strip()
    filename = request.headers.get("X-Filename", "").strip()

    try:
        total_size = int(request.headers.get("X-File-Size", "0"))
        chunk_index = int(request.headers.get("X-Chunk-Index", "0"))
        total_chunks = int(request.headers.get("X-Total-Chunks", "0"))
    except ValueError:
        return jsonify({
            "success": False,
            "message": "Invalid upload information."
        }), 400

    if not student_name:
        return jsonify({
            "success": False,
            "message": "Please enter your name."
        }), 400

    if not filename:
        return jsonify({
            "success": False,
            "message": "No file selected."
        }), 400

    if not allowed_file(filename):
        return jsonify({
            "success": False,
            "message": "Only PDF, PPT and PPTX files are supported."
        }), 400

    if total_size <= 0 or total_size > MAX_FILE_SIZE:
        return jsonify({
            "success": False,
            "message": "File is too large. Maximum size is 50 MB."
        }), 400

    if total_chunks <= 0 or chunk_index < 0 or chunk_index >= total_chunks:
        return jsonify({
            "success": False,
            "message": "Invalid upload chunk."
        }), 400

    original_ext = Path(filename).suffix.lower()

    part_filepath = os.path.join(
        UPLOAD_FOLDER,
        f"{token}.part"
    )

    original_filepath = os.path.join(
        UPLOAD_FOLDER,
        f"{token}{original_ext}"
    )

    try:

        if chunk_index == 0:

            for old_path in [
                current_session.get("filepath", ""),
                current_session.get("original_filepath", ""),
                part_filepath
            ]:

                if old_path and os.path.exists(old_path):

                    try:
                        os.remove(old_path)
                    except Exception:
                        pass

            current_session["uploading"] = True
            current_session["upload_progress"] = 0
            current_session["upload_start_time"] = time.time()
            current_session["student_name"] = student_name
            current_session["filename"] = filename

            with open(part_filepath, "wb"):
                pass

        chunk_data = request.get_data()

        with open(part_filepath, "ab") as f:
            f.write(chunk_data)

        received_size = os.path.getsize(part_filepath)

        progress = round(
            (received_size / total_size) * 100
        )

        progress = max(0, min(100, progress))

        current_session["upload_progress"] = progress

        if chunk_index < total_chunks - 1:

            return jsonify({
                "success": True,
                "complete": False,
                "progress": progress
            })

        upload_finished_time = time.time()

        upload_seconds = (
            upload_finished_time
            - current_session.get("upload_start_time", upload_finished_time)
        )

        print()
        print("========== UPLOAD TIMING ==========")
        print("File :", filename)
        print("Size :", round(total_size / (1024 * 1024), 2), "MB")
        print("Upload time :", round(upload_seconds, 2), "seconds")
        print("===================================")

        if received_size != total_size:

            current_session["uploading"] = False

            return jsonify({
                "success": False,
                "message": "Uploaded file size does not match."
            }), 400

        os.replace(
            part_filepath,
            original_filepath
        )

        filepath = original_filepath

        if original_ext in {".ppt", ".pptx"}:

            filepath = convert_presentation_to_pdf(
                original_filepath
            )

        pdf = fitz.open(filepath)

        total_pages = pdf.page_count

        pdf.close()

        if total_pages < 1:

            for cleanup_path in [
                filepath,
                original_filepath
            ]:

                if cleanup_path and os.path.exists(cleanup_path):

                    try:
                        os.remove(cleanup_path)
                    except Exception:
                        pass

            current_session["uploading"] = False
            current_session["upload_progress"] = 0

            return jsonify({
                "success": False,
                "message": "Presentation has no pages."
            }), 400

        current_session["student_name"] = student_name
        current_session["filename"] = filename
        current_session["filepath"] = filepath
        current_session["original_filepath"] = original_filepath
        current_session["total_pages"] = total_pages
        current_session["current_page"] = 1
        current_session["connected"] = True
        current_session["upload_progress"] = 100
        current_session["uploading"] = False

        print()
        print("======================================")
        print(" STUDENT CONNECTED")
        print("======================================")
        print("Student :", student_name)
        print("File    :", filename)
        print("Type    :", original_ext.upper())
        print("Pages   :", total_pages)
        print("======================================")
        print()

        return jsonify({
            "success": True,
            "complete": True,
            "progress": 100,
            "message": "Presentation uploaded successfully.",
            "redirect": f"/student/{token}"
        })

    except Exception as e:

        if os.path.exists(part_filepath):

            try:
                os.remove(part_filepath)
            except Exception:
                pass

        current_session["uploading"] = False
        current_session["upload_progress"] = 0

        return jsonify({
            "success": False,
            "message": f"Could not upload presentation: {str(e)}"
        }), 500


# GET SESSION STATE
# =========================

@app.route("/api/qr")
def get_qr():
    qr_image, student_url = generate_qr()
    return jsonify({
        "qr_image": qr_image,
        "student_url": student_url,
        "token": current_session["token"]
    })

@app.route("/api/state")
def get_state():

    if not current_session:
        return jsonify({
            "connected": False
        })

    return jsonify({
        "connected": current_session["connected"],
        "student_name": current_session["student_name"],
        "filename": current_session["filename"],
        "total_pages": current_session["total_pages"],
        "current_page": current_session["current_page"],
        "token": current_session["token"],
        "uploading": current_session["uploading"],
        "upload_progress": current_session["upload_progress"]
    })


# =========================
# CHANGE PAGE
# =========================

@app.route("/api/page", methods=["POST"])
def change_page():

    global current_session

    if not current_session or not current_session["connected"]:
        return jsonify({
            "success": False,
            "message": "No active presentation."
        }), 400

    data = request.get_json(silent=True) or {}

    try:
        page = int(data.get("page", 1))
    except Exception:
        page = 1

    total = current_session["total_pages"]

    if page < 1:
        page = 1

    if page > total:
        page = total

    current_session["current_page"] = page

    return jsonify({
        "success": True,
        "page": page
    })


# =========================
# SLIDE IMAGE
# =========================

@app.route("/slide/<token>/<int:page>")
def slide_image(token, page):

    if not current_session:
        return "No active session.", 404

    if token != current_session["token"]:
        return "Invalid session.", 404

    if not current_session["connected"]:
        return "No presentation uploaded.", 404

    if page < 1 or page > current_session["total_pages"]:
        return "Invalid page.", 404

    filepath = current_session["filepath"]

    if not os.path.exists(filepath):
        return "Presentation file not found.", 404

    try:

        pdf = fitz.open(filepath)

        pdf_page = pdf.load_page(page - 1)

        # Render thumbnail or main slide
        if request.args.get("thumb") == "1":

            cache_key = str(page)

            cached_image = current_session.get(
                "thumbnail_cache",
                {}
            ).get(cache_key)

            if cached_image:
                pdf.close()

                return send_file(
                    io.BytesIO(cached_image),
                    mimetype="image/jpeg"
                )

            matrix = fitz.Matrix(0.7, 0.7)

            pix = pdf_page.get_pixmap(
                matrix=matrix,
                alpha=False
            )

            image_bytes = pix.tobytes(
                "jpeg",
                jpg_quality=60
            )

            current_session.setdefault(
                "thumbnail_cache",
                {}
            )[cache_key] = image_bytes

            pdf.close()

            return send_file(
                io.BytesIO(image_bytes),
                mimetype="image/jpeg"
            )

        else:

            matrix = fitz.Matrix(3, 3)

            pix = pdf_page.get_pixmap(
                matrix=matrix,
                alpha=False
            )

            image_bytes = pix.tobytes("png")

            pdf.close()

            return send_file(
                io.BytesIO(image_bytes),
                mimetype="image/png"
            )

    except Exception as e:

        return f"Could not render slide: {str(e)}", 500


# =========================
# STUDENT LOGOUT
# =========================

@app.route("/student/<token>/logout", methods=["POST"])
def student_logout(token):

    global current_session

    if not current_session:
        return jsonify({
            "success": True
        })

    if token != current_session["token"]:
        return jsonify({
            "success": True,
            "message": "This presentation session has already ended."
        })

    cleanup_session()

    # Immediately create new QR session
    create_new_session()

    return jsonify({
        "success": True,
        "message": "Presentation ended."
    })

# MAIN SCREEN LOGOUT
# =========================

@app.route("/main/logout", methods=["POST"])
def main_logout():

    global current_session

    cleanup_session()

    # New QR for next student
    create_new_session()

    return jsonify({
        "success": True,
        "message": "Session ended."
    })


# =========================
# CLEANUP WHEN APP CLOSES
# =========================

@atexit.register
def shutdown_cleanup():

    try:
        cleanup_session()
    except Exception:
        pass


# =========================
# START
# =========================

def open_main_screen():
    import time
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5000")
if __name__ == "__main__":

    create_new_session()

    ip = get_local_ip()

    print()
    print("======================================")
    print("      QR PRESENTATION SYSTEM")
    print("======================================")
    print()
    print("Main Screen:")
    print(f"http://{ip}:{PORT}")
    print()
    print("Student URL will be shown in QR code.")
    print()
    print("Press CTRL + C to stop server.")
    print("======================================")
    print()

    # Start Cloudflare Quick Tunnel automatically
    start_cloudflare_tunnel()

    threading.Thread(target=open_main_screen, daemon=True).start()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )























