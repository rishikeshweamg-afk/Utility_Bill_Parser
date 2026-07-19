
import io
import os
import re
import pdfplumber
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from mega import Mega

app = FastAPI()

# Configures CORS so your Netlify static frontend can securely communicate with it
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, swap with your exact Netlify domain URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Optional Mega.io Storage Integration
MEGA_EMAIL = os.environ.get("MEGA_EMAIL", "rishikesh.weamg@gmail.com")
MEGA_PASSWORD = os.environ.get("MEGA_PASSWORD", "WEAMG@CLOUD")

try:
    mega = Mega()
    m = mega.login(MEGA_EMAIL, MEGA_PASSWORD)
    print("Connected to Mega.io cloud storage safely.")
except Exception as e:
    m = None
    print(f"Mega.io configuration skipped or failed: {e}")

# =========================================================
# YOUR ORIGINAL EXTRACTION RULES (Unchanged)
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

# =========================================================
# WEB NETWORK ROUTE (Replacing Local Loop Processors)
# =========================================================
@app.post("/api/process-bill")
async def process_bill(file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Please upload a valid PDF invoice.")

    try:
        # Read file bytes immediately into memory buffer
        pdf_bytes = await file.read()

        # Cloud Mega Backup Sequence
        if m:
            try:
                temp_path = f"cloud_temp_{file.filename}"
                with open(temp_path, "wb") as f:
                    f.write(pdf_bytes)
                target_folder = m.find('Good Foods Bills')
                m.upload(temp_path, target_folder[0] if target_folder else None)
                os.remove(temp_path)
            except Exception as mega_err:
                print(f"Mega backup error: {mega_err}")

        # Extract textual components using your logic
        full_text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"

        # Apply parsing operations
        parsed_results = parse_bill(full_text, file.filename)
        
        # Structure payload dynamically into a CSV string
        df = pd.DataFrame([parsed_results])
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)

        # Streams file directly down to the browser context
        response = StreamingResponse(
            iter([csv_buffer.getvalue()]), 
            media_type="text/csv"
        )
        response.headers["Content-Disposition"] = f"attachment; filename=extracted_{file.filename.replace('.pdf', '')}.csv"
        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))