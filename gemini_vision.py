import google.generativeai as genai
import logging
import json
from PIL import Image
import io

logger = logging.getLogger(__name__)

def analyze_trade_screenshot(image_bytes: bytes) -> dict:
    """
    Analyzes a trade screenshot image and extracts trade details using Gemini.

    Args:
        image_bytes: The image data in bytes.

    Returns:
        A dictionary containing the extracted trade details.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        model = genai.GenerativeModel('gemini-2.5-flash')
        prompt = """
        Analyze this trade screenshot. Return JSON with these keys:
        * ticker: Symbol (e.g. AMD)
        * type: CSP, CC, BPS, or CCS.
        * short_strike: The strike price of the option SOLD (Credit).
        * long_strike: The strike price of the option BOUGHT (if any). Null if single leg.
        * price: The Net Credit/Premium received.
        * expiry: Expiry Date (MM/DD/YYYY).
        * open_date: The date the trade was opened/filled (MM/DD/YYYY). Infer year if missing.
        """
        response = model.generate_content([prompt, img], stream=False)
        
        # Clean the response to extract the JSON part
        cleaned_response = response.text.strip().replace('```json', '').replace('```', '').strip()
        
        trade_details = json.loads(cleaned_response)
        return trade_details
    except Exception as e:
        logger.error(f"Error analyzing trade screenshot: {e}")
        return {}
