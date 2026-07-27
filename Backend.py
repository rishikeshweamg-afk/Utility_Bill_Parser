import io
import os
import re
import pdfplumber
import pandas as pd
from typing import List
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from mega import Mega

app = FastAPI()

# Configures CORS so your frontend can securely communicate with it
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows requests from Netlify, ngrok, and local environments
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"], # Allows browser to read file download name
)

# Optional Mega.io Storage Integration
MEGA_EMAIL = os.environ.get("MEGA_EMAIL", "rishikesh.weamg@gmail.com")
MEGA_PASSWORD = os.environ.get("MEGA_PASSWORD", "WEAMG@CLOUD")

m = None
try:
    mega = Mega()
    m = mega.login(MEGA_EMAIL, MEGA_PASSWORD)
    print("Connected to Mega.io cloud storage safely.")
except Exception as e:
    print(f"Mega.io configuration skipped or failed: {e}")

# =========================================================
# EXTRACTION RULES
# =========================================================
def clean_number(value):
    if value is None: return None
    return float(value.replace(",", "").strip())

def extract_money_after_label(label, text):
    pattern = rf"{re.escape(label)}.*"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match: return None
    line = match.group(0)
    dollar_amounts = re.findall(r"\$([\d,]+\.\d{2})", line)
    if dollar_amounts:
        return float(dollar_amounts[-1].replace(",", ""))
    return None

def extract_taxable_percentage(label, text):
    pattern = rf"{re.escape(label)}.*?\(([\d\.]+)%\s*Taxable\)"
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if match: return float(match.group(1))
    return None

def extract_value(pattern, text):
    match = re.search(pattern, text, re.DOTALL)
    if match: return match.group(1).strip()
    return None

def parse_bill(text, filename):
    data = {}
    data["FileName"] = filename
    data["BillDate"] = extract_value(r"Bill Date\s+Account Number.*?\n(\d{2}/\d{2}/\d{4})", text)
    data["AccountNumber"] = extract_value(r"ACCOUNT NUMBER:\s*([0-9\-]+)", text)
    data["TotalCurrentCharges"] = extract_money_after_label("Total Current Charges", text)
    data["TotalCurrentBalance"] = extract_money_after_label("Total Current Balance", text)

    total_use_match = re.search(r"Total Electric Use.*?([\d,]+)\s*KWH", text, re.DOTALL | re.IGNORECASE)
    data["TotalElectricUse"] = clean_number(total_use_match.group(1)) if total_use_match else None

    on_peak_match = re.search(r"On Peak\s+([\d,\.]+)", text, re.IGNORECASE)
    data["OnPeak"] = clean_number(on_peak_match.group(1)) if on_peak_match else None

    off_peak_match = re.search(r"Off Peak\s+([\d,\.]+)", text, re.IGNORECASE)
    data["OffPeak"] = clean_number(off_peak_match.group(1)) if off_peak_match else None

    data["CustomerCharge"] = extract_money_after_label("Customer Charge", text)
    data["EnergyOnPeakCharge"] = extract_money_after_label("Energy On-Peak", text)
    data["EnergyOffPeakCharge"] = extract_money_after_label("Energy Off-Peak", text)
    data["DemandOnPeakCharge"] = extract_money_after_label("Demand On-Peak", text)
    data["CustomerDemandCharge"] = extract_money_after_label("Customer Demand", text)
    data["EnvironmentalControlCharge"] = extract_money_after_label("Environmental Control Charge", text)
    data["StateLowIncomeAssistanceFee"] = extract_money_after_label("State Low Income Assistance Fee", text)
    data["WIStateTax"] = extract_money_after_label("WI State Tax", text)
    data["WIStateTaxPercentage"] = extract_taxable_percentage("WI State Tax", text)

    county_match = re.search(r"(WI County Sales Tax.*)", text, re.IGNORECASE)
    if county_match:
        county_line = county_match.group(1)
        amounts = re.findall(r"\$([\d,]+\.\d{2})", county_line)
        data["WICountySalesTax"] = float(amounts[-1].replace(",", "")) if amounts else None
        pct_match = re.search(r"\(([\d\.]+)%\s*Taxable\)", county_line, re.IGNORECASE)
        data["WICountySalesTaxPercentage"] = float(pct_match.group(1)) if pct_match else None
    else:
        data["WICountySalesTax"] = None
        data["WICountySalesTaxPercentage"] = None

    return data

# Helper function to extract text from PDF bytes
def process_pdf_bytes(pdf_bytes, filename):
    # Cloud Mega Backup Sequence
    if m:
        try:
            temp_path = f"cloud_temp_{filename}"
            with open(temp_path, "wb") as f:
                f.write(pdf_bytes)
            target_folder = m.find('Good Foods Bills')
            m.upload(temp_path, target_folder[0] if target_folder else None)
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception as mega_err:
            print(f"Mega backup error (non-fatal): {mega_err}")

    # Extract textual components using pdfplumber
    full_text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                full_text += page_text + "\n"

    return parse_bill(full_text, filename)

# =========================================================
# WEB NETWORK ROUTES
# =========================================================

# BATCH MULTI-FILE ROUTE (Combines multiple bills into ONE CSV)
@app.post("/api/process-bills-batch")
@app.post("/extract_bills_batch/")
async def process_bills_batch(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    combined_results = []

    try:
        for file in files:
            if not file.filename.lower().endswith('.pdf'):
                continue # Skip non-PDF files safely

            pdf_bytes = await file.read()
            parsed_data = process_pdf_bytes(pdf_bytes, file.filename)
            combined_results.append(parsed_data)

        if not combined_results:
            raise HTTPException(status_code=400, detail="No valid PDF invoices were found in the uploaded batch.")

        # Create a single combined DataFrame from all parsed results
        df = pd.DataFrame(combined_results)
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)

        # Stream single consolidated CSV back to client
        response = StreamingResponse(
            iter([csv_buffer.getvalue()]), 
            media_type="text/csv"
        )
        response.headers["Content-Disposition"] = "attachment; filename=consolidated_bills_report.csv"
        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# SINGLE FILE ROUTE (Preserved for backwards compatibility)
@app.post("/api/process-bill")
@app.post("/extract_bill/")
async def process_bill(file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Please upload a valid PDF invoice.")

    try:
        pdf_bytes = await file.read()
        parsed_results = process_pdf_bytes(pdf_bytes, file.filename)
        
        df = pd.DataFrame([parsed_results])
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)

        clean_filename = file.filename.replace('.pdf', '')
        response = StreamingResponse(
            iter([csv_buffer.getvalue()]), 
            media_type="text/csv"
        )
        response.headers["Content-Disposition"] = f"attachment; filename=extracted_{clean_filename}.csv"
        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))