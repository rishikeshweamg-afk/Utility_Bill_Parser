import io
import os
import re
import fitz  # PyMuPDF
import asyncio
import pdfplumber
import pytesseract
import pandas as pd
from PIL import Image
from typing import List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from mega import Mega

app = FastAPI()

# Configures CORS so your frontend can securely communicate with it
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows requests from Netlify, ngrok, and local environments
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],  # Allows browser to read file download name
)

# Configure Tesseract path (Update if hosted on a server or different path)
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.exists(TESSERACT_PATH):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

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
# SHARED HELPERS & UTILITIES
# =========================================================
def clean_number(value):
    if value is None:
        return None
    return float(str(value).replace(",", "").replace("$", "").strip())

def extract_last_dollar_value(line):
    if not line:
        return None
    amounts = re.findall(r"-?\$[\d,]+\.\d{2}", line)
    if amounts:
        return clean_number(amounts[-1])
    return None

def extract_line(pattern, text):
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(0)
    return None

def extract_money_after_label(label, text):
    pattern = rf"{re.escape(label)}.*"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    line = match.group(0)
    dollar_amounts = re.findall(r"\$([\d,]+\.\d{2})", line)
    if dollar_amounts:
        return float(dollar_amounts[-1].replace(",", ""))
    return None

def extract_taxable_percentage(label, text):
    pattern = rf"{re.escape(label)}.*?\(([\d\.]+)%\s*Taxable\)"
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if match:
        return float(match.group(1))
    return None

def extract_value(pattern, text):
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


# =========================================================
# WE ENERGIES ELECTRIC PARSER
# =========================================================
def parse_we_energies_bill(text, filename):
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
# WE ENERGIES GAS PARSER
# =========================================================
def parse_we_energies_gas_bill(text, filename):
    data = {}
    data["FileName"] = filename

    bill_date_match = re.search(r"Bill Date.*?\n(\d{2}/\d{2}/\d{4})", text, re.DOTALL)
    data["BillDate"] = bill_date_match.group(1) if bill_date_match else None

    account_match = re.search(r"ACCOUNT NUMBER:\s*([0-9\-]+)", text)
    data["AccountNumber"] = account_match.group(1) if account_match else None

    total_current_charges_match = re.search(r"Total Current Charges\s+\$([\d,]+\.\d{2})", text)
    data["TotalCurrentCharges"] = clean_number(total_current_charges_match.group(1)) if total_current_charges_match else None

    total_current_balance_match = re.search(r"Total Current Balance\s+\$([\d,]+\.\d{2})", text)
    data["TotalCurrentBalance"] = clean_number(total_current_balance_match.group(1)) if total_current_balance_match else None

    total_gas_use_match = re.search(r"Total Gas Use.*?(\d+)\s+CCF", text, re.DOTALL)
    data["TotalGasUse"] = clean_number(total_gas_use_match.group(1)) if total_gas_use_match else None

    btu_match = re.search(r"CCF x ([\d\.]+)\s+BTU", text)
    data["BTU"] = float(btu_match.group(1)) if btu_match else None

    therms_match = re.search(r"=\s*([\d,\.]+)\s+Therms", text)
    data["Therms"] = clean_number(therms_match.group(1)) if therms_match else None

    customer_charge_line = extract_line(r"\n\s*Customer Charge\s+.*", text)
    data["CustomerCharge"] = extract_last_dollar_value(customer_charge_line) if customer_charge_line else None

    distribution_line = extract_line(r"\n\s*Distribution\s+.*", text)
    data["Distribution"] = extract_last_dollar_value(distribution_line) if distribution_line else None

    base_gas_line = extract_line(r"\n\s*Base Gas\s+.*", text)
    data["BaseGas"] = extract_last_dollar_value(base_gas_line) if base_gas_line else None

    pga_lines = re.findall(r"\n\s*PGA\s+.*", text, re.IGNORECASE)
    pga_values = []
    for line in pga_lines:
        val = extract_last_dollar_value(line)
        if val is not None:
            pga_values.append(val)

    for i, val in enumerate(pga_values, start=1):
        data[f"PGA{i}"] = val

    for i in range(len(pga_values) + 1, 6):
        data[f"PGA{i}"] = None

    state_tax_line = extract_line(r"\n\s*WI State Tax\s+.*", text)
    data["WIStateTax"] = extract_last_dollar_value(state_tax_line) if state_tax_line else None

    county_tax_line = extract_line(r"\n\s*WI County Sales Tax Kenosha\s+.*", text)
    data["WICountySalesTaxKenosha"] = extract_last_dollar_value(county_tax_line) if county_tax_line else None

    subtotal_match = re.search(r"Subtotal:\s+\$([\d,]+\.\d{2})", text)
    data["Subtotal"] = clean_number(subtotal_match.group(1)) if subtotal_match else None

    return data


# =========================================================
# ALLIANT ENERGY EXTRACTION RULES & OCR LOGIC
# =========================================================
def clean_money_alliant(value):
    if value is None:
        return ""
    return value.replace("$", "").replace(",", "").replace("CR", "").strip()

def get_last_dollar_alliant(line):
    amounts = re.findall(r'\$([\d,]+\.\d{2})', line)
    if amounts:
        return amounts[-1].replace(",", "")
    return ""

def extract_multiple_alliant(text, keyword):
    values = []
    for line in text.splitlines():
        if keyword.lower() in line.lower():
            value = get_last_dollar_alliant(line)
            if value:
                values.append(value)
    return values

def extract_alliant_summary_fields(text):
    data = {}

    match = re.search(r'Account Number\s+(\d+)', text, re.IGNORECASE)
    data["Account_Number"] = match.group(1) if match else ""

    match = re.search(r'Account Name:\s*(.+)', text, re.IGNORECASE)
    data["Account_Name"] = match.group(1).strip() if match else ""

    match = re.search(r'Bill Date\s+([A-Za-z]{3}\s+\d{1,2},\s+\d{4})', text, re.IGNORECASE)
    data["Bill_Date"] = match.group(1) if match else ""

    amount_patterns = [
        r'Amount Due\s*\$([\d,]+\.\d{2})',
        r'Total Amount Due\s*\$([\d,]+\.\d{2})',
        r'Balance Due\s*\$([\d,]+\.\d{2})'
    ]
    data["Amount_Due"] = ""
    for pattern in amount_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            data["Amount_Due"] = clean_money_alliant(match.group(1))
            break

    match = re.search(r'Total Current Charges\s*\$([\d,]+\.\d{2})', text, re.IGNORECASE)
    data["Total_Current_Charges"] = clean_money_alliant(match.group(1)) if match else ""

    all_kwh = re.findall(r'(\d[\d,]*)\s*kWh', text, re.IGNORECASE)
    if all_kwh:
        data["Total_Electric_Use_KWH"] = max([int(x.replace(",", "")) for x in all_kwh])
    else:
        data["Total_Electric_Use_KWH"] = ""

    match = re.search(r'High Rate.*?(\d[\d,]*)\s*kWh', text, re.IGNORECASE | re.DOTALL)
    data["High_Rate_KWH"] = match.group(1).replace(",", "") if match else ""

    match = re.search(r'Low Rate.*?(\d[\d,]*)\s*kWh', text, re.IGNORECASE | re.DOTALL)
    data["Low_Rate_KWH"] = match.group(1).replace(",", "") if match else ""

    match = re.search(r'Regular Rate.*?(\d[\d,]*)\s*kWh', text, re.IGNORECASE | re.DOTALL)
    data["Regular_Rate_KWH"] = match.group(1).replace(",", "") if match else ""

    match = re.search(r'On[- ]Peak Demand.*?([\d\.]+)', text, re.IGNORECASE)
    data["On_Peak_Demand_KW"] = match.group(1) if match else ""

    match = re.search(r'Off[- ]Peak Demand.*?([\d\.]+)', text, re.IGNORECASE)
    data["Off_Peak_Demand_KW"] = match.group(1) if match else ""

    return data

def extract_alliant_charge_fields(text):
    data = {}

    values = extract_multiple_alliant(text, "High Energy Charge")
    data["High_Energy_Charge_1"] = values[0] if len(values) > 0 else ""
    data["High_Energy_Charge_2"] = values[1] if len(values) > 1 else ""

    values = extract_multiple_alliant(text, "Regular Energy Charge")
    data["Regular_Energy_Charge_1"] = values[0] if len(values) > 0 else ""
    data["Regular_Energy_Charge_2"] = values[1] if len(values) > 1 else ""

    values = extract_multiple_alliant(text, "Low Energy Charge")
    data["Low_Energy_Charge_1"] = values[0] if len(values) > 0 else ""
    data["Low_Energy_Charge_2"] = values[1] if len(values) > 1 else ""

    values = extract_multiple_alliant(text, "On Peak Demand")
    data["On_Peak_Demand_Charge_1"] = values[0] if len(values) > 0 else ""
    data["On_Peak_Demand_Charge_2"] = values[1] if len(values) > 1 else ""

    values = extract_multiple_alliant(text, "Customer Demand Charge")
    data["Customer_Demand_Charge_1"] = values[0] if len(values) > 0 else ""
    data["Customer_Demand_Charge_2"] = values[1] if len(values) > 1 else ""

    values = extract_multiple_alliant(text, "Customer Charge")
    data["Customer_Charge_1"] = values[0] if len(values) > 0 else ""
    data["Customer_Charge_2"] = values[1] if len(values) > 1 else ""

    values = extract_multiple_alliant(text, "2021 Fuel Adjustment")
    data["Fuel_Adjustment_2021_1"] = values[0] if len(values) > 0 else ""
    data["Fuel_Adjustment_2021_2"] = values[1] if len(values) > 1 else ""

    values = extract_multiple_alliant(text, "2022 Fuel Adjustment")
    data["Fuel_Adjustment_2022_1"] = values[0] if len(values) > 0 else ""
    data["Fuel_Adjustment_2022_2"] = values[1] if len(values) > 1 else ""

    match = re.search(r'State-Wide Low-Income Assistance Fee.*?\$([\d,]+\.\d{2})', text, re.IGNORECASE | re.DOTALL)
    data["State_Wide_Low_Income_Assistance_Fee"] = clean_money_alliant(match.group(1)) if match else ""

    county_match = re.search(r'County Tax.*?([\d\.]+)%.*?\$([\d,]+\.\d{2})', text, re.IGNORECASE | re.DOTALL)
    if county_match:
        data["County_Tax_Percentage"] = county_match.group(1)
        data["County_Tax"] = county_match.group(2).replace(",", "")
    else:
        data["County_Tax_Percentage"] = ""
        data["County_Tax"] = ""

    state_match = re.search(r'Wisconsin Sales Tax.*?([\d\.]+)%.*?\$([\d,]+\.\d{2})', text, re.IGNORECASE | re.DOTALL)
    if state_match:
        data["Wisconsin_Sales_Tax_Percentage"] = state_match.group(1)
        data["Wisconsin_Sales_Tax"] = state_match.group(2).replace(",", "")
    else:
        data["Wisconsin_Sales_Tax_Percentage"] = ""
        data["Wisconsin_Sales_Tax"] = ""

    return data


# =========================================================
# WORKER DISPATCHER LOGIC
# =========================================================

def process_pdf_bytes_sync(pdf_bytes: bytes, filename: str, vendor: str, utility_type: str = "electric") -> dict:
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

    # Process based on selected vendor and utility type
    if vendor == "alliant_energy":
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        full_text = ""
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            pix = page.get_pixmap(dpi=300)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text = pytesseract.image_to_string(img)
            full_text += text + "\n"

        row = {"FileName": filename}
        row.update(extract_alliant_summary_fields(full_text))
        row.update(extract_alliant_charge_fields(full_text))
        return row

    elif vendor == "we_energies" and utility_type.lower() == "gas":
        full_text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"

        return parse_we_energies_gas_bill(full_text, filename)

    else:
        # Default: We Energies Electric text extraction
        full_text = ""
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"

        return parse_we_energies_bill(full_text, filename)


# =========================================================
# WEB NETWORK ROUTES
# =========================================================

# BATCH MULTI-FILE ROUTE (Combines multiple bills into ONE CSV)
@app.post("/api/process-bills-batch")
@app.post("/extract_bills_batch/")
async def process_bills_batch(
    vendor: str = Form("we_energies"),
    utility_type: str = Form("electric"),
    files: List[UploadFile] = File(...)
):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    combined_results = []

    try:
        for file in files:
            if not file.filename.lower().endswith('.pdf'):
                continue  # Skip non-PDF files safely

            pdf_bytes = await file.read()
            # Offload heavy OCR/extraction parsing to non-blocking thread
            parsed_data = await asyncio.to_thread(
                process_pdf_bytes_sync, pdf_bytes, file.filename, vendor, utility_type
            )
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
        response.headers["Content-Disposition"] = f"attachment; filename=consolidated_bills_{vendor}_{utility_type}.csv"
        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# SINGLE FILE ROUTE (Preserved for backwards compatibility)
@app.post("/api/process-bill")
@app.post("/extract_bill/")
async def process_bill(
    vendor: str = Form("we_energies"),
    utility_type: str = Form("electric"),
    file: UploadFile = File(...)
):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="Please upload a valid PDF invoice.")

    try:
        pdf_bytes = await file.read()
        parsed_results = await asyncio.to_thread(
            process_pdf_bytes_sync, pdf_bytes, file.filename, vendor, utility_type
        )
        
        df = pd.DataFrame([parsed_results])
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)

        clean_filename = file.filename.replace('.pdf', '')
        response = StreamingResponse(
            iter([csv_buffer.getvalue()]), 
            media_type="text/csv"
        )
        response.headers["Content-Disposition"] = f"attachment; filename=extracted_{clean_filename}_{vendor}_{utility_type}.csv"
        return response

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))