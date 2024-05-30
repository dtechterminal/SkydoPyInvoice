# SkydoPyInvoice

`SkydoPyInvoice` is a Python module designed for interfacing with the Skydo invoicing system. It automates the creation, management, and finalization of invoices through the Skydo Dashboard API. This module allows users to programmatically add clients, select invoice items, and manage invoice details with a simple, straightforward Python class.

## Features

- **Session Management**: Handles user sessions using cookies for authentication.
- **Invoice Creation**: Automatically generates invoices with unique identifiers.
- **Client and Item Management**: Allows adding and selecting clients and items dynamically based on input parameters.
- **Detail Configuration**: Supports custom configurations for invoices including GST handling, addresses, and more.
- **Logging**: Comprehensive logging for debugging and tracking the flow of data and actions.

## Prerequisites

Before you can use the `SkydoPyInvoice`, you need to install Python 3 and the following Python packages:

- `requests`: For making HTTP requests.
- `datetime`: For handling date and time operations.

You can install the required packages using pip:

```bash
pip install requests
```

## Installation

Clone this repository to your local machine:

```bash
git clone https://github.com/dtechterminal/SkydoPyInvoice.git
```

Navigate to the cloned directory:

```bash
cd SkydoPyInvoice
```

## Usage

To use the `SkydoPyInvoice` module, you need to import the class from the script and initialize it with the necessary parameters:

```python
from skydo_api import SkydoAPI

# Initialize the API with cookie string, client name, and item name
api = SkydoAPI(cookie_str='YOUR_COOKIE_STRING', client_name='Client Name', item_name='Item Name')
```

### Parameters

- `cookie_str`: String. The cookies obtained after authentication with the Skydo system.
- `client_name`: String. The name of the client for which the invoice is being created.
- `item_name`: String. The name of the item to add to the invoice.
- `year`: (Optional) Integer. The year for the invoice (defaults to the current year).
- `month`: (Optional) Integer. The month for the invoice (defaults to the current month).

### Methods

- `create_invoice()`: Creates a new invoice.
- `get_invoice_details()`: Retrieves details of the current invoice.
- `choose_client(client)`: Selects the client for the invoice.
- `choose_items(item)`: Adds items to the invoice.
- `other_details()`: Adds other miscellaneous details to the invoice.
- `finalize_invoice()`: Finalizes the invoice.

## Contribution

Contributions to the `SkydoPyInvoice` project are welcome. Please follow these steps to contribute:

1. Fork the repository.
2. Create a new branch (`git checkout -b feature-branch`).
3. Make your changes.
4. Commit your changes (`git commit -am 'Add some feature'`).
5. Push to the branch (`git push origin feature-branch`).
6. Create a new Pull Request.
