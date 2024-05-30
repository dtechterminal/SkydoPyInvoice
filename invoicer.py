import requests
import logging
from datetime import datetime, timedelta

def get_invoice_items(year, month, rate_per_hour, name):
    # Function to find the first weekday of a given month and year that's not Saturday or Sunday
    def get_first_weekday(year, month):
        first_day = datetime(year, month, 1)
        # Adjust if the first day is Saturday (5) or Sunday (6)
        if first_day.weekday() == 5:
            first_day += timedelta(days=2)
        elif first_day.weekday() == 6:
            first_day += timedelta(days=1)
        return first_day

    # Function to create the description for each week
    def create_week_description(start_date, end_date):
        return f"{start_date.strftime('%m-%d-%Y')} - {end_date.strftime('%m-%d-%Y')}"

    # Function to generate the dictionary for each week
    def generate_weekly_schedule(year, month, rate_per_hour):
        schedule_list = []
        first_weekday = get_first_weekday(year, month)
        current_date = first_weekday

        while current_date.month == month:
            days_in_week = 0
            week_end_date = current_date

            # Count only weekdays (Monday to Friday)
            while week_end_date.weekday() < 5 and week_end_date.month == month:
                days_in_week += 1
                week_end_date += timedelta(days=1)

            week_end_date -= timedelta(days=1)  # Adjust end_date to the last weekday

            description = create_week_description(current_date, week_end_date)
            quantity = 8 * days_in_week
            total = quantity * rate_per_hour

            schedule_dict = {
                "quantity": quantity,
                "igstApplicable": True,
                "description": description,
                "sac": None,
                "rate": rate_per_hour,
                "igst": None,
                "cgst": None,
                "sgst": None,
                "name": name,
                "unit": "HOUR",
                "total": total
            }
            schedule_list.append(schedule_dict)
            current_date = week_end_date + timedelta(days=3)  # Move to the next Monday

        return schedule_list

    weekly_schedule = generate_weekly_schedule(year, month, rate_per_hour)
    return [schedule for schedule in weekly_schedule]

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class SkydoAPI:
    BASE_URL = "https://dashboard.skydo.com/api/"
    invoice_id = 0
    clients = []
    invoice_items = []
    year = 2024
    month = 4

    def __init__(self, cookie_str, client_name, item_name, year=None, month=None):
        logging.info("Initializing SkydoAPI with client_name=%s, item_name=%s, year=%s, month=%s", client_name, item_name, year, month)
        if not year:
            current_date = datetime.now()
            self.year = current_date.year
            self.month = current_date.month

        self.session = requests.Session()
        self.session.headers.update({'x-server': 'CHALLAN'})
        self.load_cookies(cookie_str)
        self.create_invoice()
        self.get_invoice_details()
        for client in self.clients:
            if client_name.lower() == client['name'].lower():
                self.choose_client(client)
                break
        for item in self.invoice_items:
            if item_name.lower() == item['name'].lower():
                self.choose_items(item)
                break
        self.other_details()
        self.finalize_invoice()

    def load_cookies(self, cookie_str):
        logging.info("Loading cookies")
        cookies = {c.split('=')[0]: c.split('=')[1] for c in cookie_str.split('; ')}
        for name, value in cookies.items():
            self.session.cookies.set(name, value)

    def create_invoice(self):
        logging.info("Creating invoice")
        endpoint = "route?path=challan/create/invoice"
        url = f"{self.BASE_URL}/{endpoint}"
        response = self.session.post(url).json()
        self.invoice_id = response['data']
        logging.info("Invoice created with ID=%s", self.invoice_id)

    def get_invoice_details(self):
        logging.info("Getting invoice details for invoice_id=%s", self.invoice_id)
        endpoint = "create-invoice/get-invoice-details?invoiceId=" + str(self.invoice_id)
        url = f"{self.BASE_URL}/{endpoint}"
        response = self.session.get(url).json()
        self.clients = response['data']['cacheDetails']['challanClients']
        self.invoice_items = response['data']['cacheDetails']['invoiceItems']
        logging.info("Loaded clients and items from invoice details")

    def choose_client(self, client):
        logging.info("Choosing client: %s", client['name'])
        endpoint = "create-invoice/update-invoice?path=challan/update/bill/to&invoiceId=" + str(self.invoice_id)
        url = f"{self.BASE_URL}/{endpoint}"
        payload = {
            "invoiceId": self.invoice_id,
            "name": client['name'],
            "address": client['address'].replace('\n',' '),
            "pincode": "",
            "country": client['country'],
            "gstin": "",
            "placeOfSupply": "Other country(96)",
            "gstinNotAvailable": False,
            "gstinVerified": False
        }
        self.session.post(url, json=payload).json()

    def choose_items(self, item):
        logging.info("Choosing Item: %s", item['name'])
        endpoint = "create-invoice/update-invoice?path=/challan/update/items&invoiceId=" + str(self.invoice_id)
        url = f"{self.BASE_URL}/{endpoint}"
        invoice_items = get_invoice_items(self.year, self.month, item['rate'], item['name'])
        payload = {
            "invoiceId": self.invoice_id,
            "invoiceFinancial": {
                "currency": item['currency'],
                "subTotal": sum(item['total'] for item in invoice_items),
                "discountPercentage": 0,
                "discountValue": None,
                "platformFees": None,
                "total": sum(item['total'] for item in invoice_items),
                "totalCgst": 0,
                "totalSgst": 0,
                "totalIgst": 0,
                "igstApplicable": True
            },
            "invoiceItems": invoice_items
        }
        self.session.post(url, json=payload).json()

    def other_details(self):
        logging.info("Updating other invoice details.")
        endpoint = "create-invoice/update-invoice?path=/challan/update/other/details&invoiceId=" + str(self.invoice_id)
        url = f"{self.BASE_URL}/{endpoint}"
        payload = {
            "invoiceId": self.invoice_id,
            "includeLut": True,
            "includeSignature": True,
            "notes": None,
            "lut": None,
            "others": {}
        }
        self.session.post(url, json=payload).json()
        logging.debug("Other details updated.")
