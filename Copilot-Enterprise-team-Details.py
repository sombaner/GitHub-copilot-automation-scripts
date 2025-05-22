import json
import logging
import os
import requests
import csv
import time
import datetime
from datetime import datetime, timedelta
import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.communication.email import EmailClient
from azure.core.exceptions import ResourceExistsError
from azure.keyvault.secrets import SecretClient

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Configuration variables - would usually be stored in Azure Key Vault or as application settings
# The function uses environment variables to get configuration
def get_config():
    return {
        'enterprise_slug': os.environ.get('GITHUB_ENTERPRISE_SLUG'),
        'blob_storage_connection_string': os.environ.get('AzureWebJobsStorage'),
        'container_name': os.environ.get('BLOB_CONTAINER_NAME', 'copilot-reports'),
        'email_list_blob_path': os.environ.get('EMAIL_LIST_BLOB_PATH', 'config/emails.json'),
        'key_vault_name': os.environ.get('KEY_VAULT_NAME'),
        'github_auth_token_secret_name': os.environ.get('GITHUB_AUTH_TOKEN_SECRET_NAME', 'github-auth-token'),
        'communication_service_connection_string': os.environ.get('COMMUNICATION_SERVICE_CONNECTION_STRING'),
        'sender_email': os.environ.get('SENDER_EMAIL', 'copilot_report@example.com')
    }

# Main function that runs as an Azure Function
def main(mytimer: func.TimerRequest) -> None:
    """
    Azure Function entry point that is triggered on a timer schedule.
    
    Parameters:
        mytimer: The timer trigger that invoked this function
    """
    logging.info('Python timer trigger function started')

    # Check if function ran due to missed schedule
    if mytimer.past_due:
        logging.info('The timer is past due!')

    try:
        config = get_config()
        
        # Validate configuration
        required_configs = ['enterprise_slug', 'key_vault_name']
        missing_configs = [key for key in required_configs if not config.get(key)]
        if missing_configs:
            raise ValueError(f"Missing required configuration values: {', '.join(missing_configs)}")

        # Get GitHub auth token from Key Vault
        auth_token = get_auth_token_from_key_vault(
            config['key_vault_name'], 
            config['github_auth_token_secret_name']
        )

        # Execute the main workflow
        teams = fetch_teams(config['enterprise_slug'], auth_token)
        seats_info = get_copilot_billing_seats(config['enterprise_slug'], auth_token, teams)
        
        # Generate filename with date
        today = datetime.now().strftime("%Y_%m_%d")
        filename = f'copilot_billing_seats_{today}.csv'
        local_file_path = f'/tmp/{filename}'
        
        # Save data to CSV locally first, then to Azure Blob Storage
        save_to_csv(seats_info, local_file_path)
        
        # Upload to Azure Blob Storage
        upload_to_blob_storage(
            config['blob_storage_connection_string'],
            config['container_name'],
            local_file_path,
            filename
        )
        
        # Send email report
        send_email(
            config['blob_storage_connection_string'],
            config['container_name'],
            config['email_list_blob_path'],
            config['communication_service_connection_string'],
            config['sender_email'],
            local_file_path,
            filename
        )
        
        logging.info(f'Successfully processed {len(seats_info)} Copilot seats')
        
    except Exception as e:
        logging.error(f"Error in main function: {str(e)}", exc_info=True)
        # In a production environment, we would want to alert on failures
        # Here we could add application insights or other monitoring

def get_auth_token_from_key_vault(key_vault_name, secret_name):
    """
    Retrieves the GitHub authentication token from Azure Key Vault.
    
    Parameters:
        key_vault_name: The name of the Azure Key Vault
        secret_name: The name of the secret containing the GitHub token
        
    Returns:
        The GitHub authentication token
    """
    try:
        logging.info(f"Retrieving GitHub auth token from Key Vault: {key_vault_name}")
        # Use DefaultAzureCredential for authentication, which works with managed identity in production
        # and falls back to other methods (e.g., environment variables) when developing locally
        credential = DefaultAzureCredential()
        key_vault_url = f"https://{key_vault_name}.vault.azure.net/"
        secret_client = SecretClient(vault_url=key_vault_url, credential=credential)
        
        # Get the secret containing the GitHub token
        secret = secret_client.get_secret(secret_name)
        return secret.value
    except Exception as e:
        logging.error(f"Error retrieving auth token from Key Vault: {str(e)}", exc_info=True)
        raise

def fetch_teams(enterprise_slug, token, max_retries=3):
    """
    Fetches all teams in the enterprise with pagination handling and retry logic.
    
    Parameters:
        enterprise_slug: The GitHub enterprise slug identifier
        token: The GitHub authentication token
        max_retries: Maximum number of retry attempts for transient errors
        
    Returns:
        List of teams with their IDs and names
    """
    url = f"https://api.github.com/enterprises/{enterprise_slug}/teams?per_page=100"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    teams = []
    retry_count = 0
    
    while url:
        try:
            logging.info(f"Fetching teams from URL: {url}")
            response = requests.get(url, headers=headers)
            
            # Check for rate limiting
            check_rate_limit(response.headers)
            
            if response.status_code == 200:
                data = response.json()
                teams.extend([{'id': team['id'], 'name': team['name']} for team in data])
                
                # Handle pagination using the Link header
                url = response.links.get('next', {}).get('url')
                if url:
                    logging.info("Fetching next page of teams...")
                
                # Reset retry counter on successful request
                retry_count = 0
            else:
                logging.error(f"Failed to fetch teams with status code {response.status_code}. Error: {response.text}")
                if response.status_code >= 500 and retry_count < max_retries:
                    retry_count += 1
                    wait_time = 2 ** retry_count  # Exponential backoff
                    logging.info(f"Retrying in {wait_time} seconds... (Attempt {retry_count} of {max_retries})")
                    time.sleep(wait_time)
                else:
                    break  # Stop the loop if max retries reached or non-retryable error
        except requests.exceptions.RequestException as e:
            logging.error(f"Request error while fetching teams: {str(e)}")
            if retry_count < max_retries:
                retry_count += 1
                wait_time = 2 ** retry_count  # Exponential backoff
                logging.info(f"Retrying in {wait_time} seconds... (Attempt {retry_count} of {max_retries})")
                time.sleep(wait_time)
            else:
                break  # Stop the loop if max retries reached

    logging.info(f"Fetched {len(teams)} teams successfully.")
    return teams

def get_user_details(username, auth_token, max_retries=3):
    """
    Fetches details like email and created_at for a given GitHub username with retry logic.
    
    Parameters:
        username: The GitHub username
        auth_token: The GitHub authentication token
        max_retries: Maximum number of retry attempts for transient errors
        
    Returns:
        Tuple containing email and created_at date
    """
    user_api_url = f"https://api.github.com/users/{username}"
    headers = {
        "Authorization": f"Bearer {auth_token}"
    }
    
    retry_count = 0
    while retry_count <= max_retries:
        try:
            response = requests.get(user_api_url, headers=headers)
            
            # Check for rate limiting
            check_rate_limit(response.headers)
            
            if response.status_code == 200:
                user_data = response.json()
                email = user_data.get('email') or 'N/A'
                created_at = user_data.get('created_at') or 'N/A'
                return email, created_at
            else:
                logging.error(f"Failed to fetch details for {username}: {response.status_code} - {response.text}")
                
                # Retry for server errors (5xx)
                if response.status_code >= 500 and retry_count < max_retries:
                    retry_count += 1
                    wait_time = 2 ** retry_count  # Exponential backoff
                    logging.info(f"Retrying in {wait_time} seconds... (Attempt {retry_count} of {max_retries})")
                    time.sleep(wait_time)
                else:
                    return 'N/A', 'N/A'
        except requests.exceptions.RequestException as e:
            logging.error(f"Request error while fetching user details for {username}: {str(e)}")
            if retry_count < max_retries:
                retry_count += 1
                wait_time = 2 ** retry_count  # Exponential backoff
                logging.info(f"Retrying in {wait_time} seconds... (Attempt {retry_count} of {max_retries})")
                time.sleep(wait_time)
            else:
                return 'N/A', 'N/A'
    
    return 'N/A', 'N/A'  # Default return if all retries fail

def check_rate_limit(headers):
    """
    Check and handle GitHub API rate limiting.
    
    Parameters:
        headers: The HTTP response headers from a GitHub API request
    """
    remaining = int(headers.get('X-RateLimit-Remaining', 0))
    reset_time = int(headers.get('X-RateLimit-Reset', 0))
    
    if remaining == 0:
        wait_time = max(reset_time - time.time(), 0)
        logging.warning(f"Rate limit reached. Waiting for {wait_time} seconds.")
        time.sleep(wait_time + 1)  # Wait until the reset time plus a buffer

def get_copilot_billing_seats(enterprise_slug, auth_token, teams, max_retries=3):
    """
    Fetches the Copilot billing seats data and processes it for each team with retry logic.
    
    Parameters:
        enterprise_slug: The GitHub enterprise slug identifier
        auth_token: The GitHub authentication token
        teams: List of teams to check for
        max_retries: Maximum number of retry attempts for transient errors
        
    Returns:
        List of user information dictionaries
    """
    api_url = f"https://api.github.com/enterprises/{enterprise_slug}/copilot/billing/seats"
    headers = {
        "Authorization": f"Bearer {auth_token}"
    }

    users_info = []
    page = 1
    retry_count = 0
    
    while True:
        logging.info(f"Fetching page {page} of Copilot billing seats.")
        
        try:
            response = requests.get(api_url, headers=headers, params={'page': page})
            
            # Check for rate limiting
            check_rate_limit(response.headers)
            
            if response.status_code == 200:
                # Reset retry counter on successful request
                retry_count = 0
                
                data = response.json()

                # If no data is returned, break the loop
                if not data['seats']:
                    break

                for item in data['seats']:
                    assigning_team = item.get('assigning_team', {})
                    assignee = item.get('assignee', {})
                    team_name = assigning_team.get('name', 'N/A')
                    
                    if team_name in [team['name'] for team in teams] and assignee.get('login'):
                        username = assignee.get('login')
                        
                        # Fetch email and created_at using another API call
                        email, created_at = get_user_details(username, auth_token)
                        
                        last_activity_at = item.get('last_activity_at') or 'N/A'
                        
                        # Extract last_activity_editor data correctly
                        last_activity_editor = item.get('last_activity_editor') or 'N/A'
                        logging.info(f"Last Activity Editor for {username}: {last_activity_editor}")
                        
                        # Split into components if required
                        parts = last_activity_editor.split('/')
                        last_active_editor = parts[0] if len(parts) > 0 else 'N/A'
                        editor_version = parts[1] if len(parts) > 1 else 'N/A'
                        plugin = parts[2] if len(parts) > 2 else 'N/A'
                        plugin_version = parts[3] if len(parts) > 3 else 'N/A'

                        # Extract team slug
                        team_slug = assigning_team.get('slug') or 'N/A'
                        
                        users_info.append({
                            'Username': username or 'N/A', 
                            'Email': email, 
                            'Created At': created_at,
                            'Last Activity At': last_activity_at,
                            'Last Active Editor': last_active_editor,
                            'Editor Version': editor_version,
                            'Plugin': plugin,
                            'Plugin Version': plugin_version,
                            'Team Slug': team_slug
                        })
                
                page += 1
            else:
                logging.error(f"Error fetching page {page}: {response.status_code} - {response.text}")
                
                # Retry for server errors (5xx)
                if response.status_code >= 500 and retry_count < max_retries:
                    retry_count += 1
                    wait_time = 2 ** retry_count  # Exponential backoff
                    logging.info(f"Retrying in {wait_time} seconds... (Attempt {retry_count} of {max_retries})")
                    time.sleep(wait_time)
                else:
                    break  # Stop the loop if max retries reached or non-retryable error
        except requests.exceptions.RequestException as e:
            logging.error(f"Request error while fetching billing seats (page {page}): {str(e)}")
            if retry_count < max_retries:
                retry_count += 1
                wait_time = 2 ** retry_count  # Exponential backoff
                logging.info(f"Retrying in {wait_time} seconds... (Attempt {retry_count} of {max_retries})")
                time.sleep(wait_time)
            else:
                break  # Stop the loop if max retries reached

    return users_info

def save_to_csv(data, file_path):
    """
    Save the Copilot billing seats data to a CSV file.
    
    Parameters:
        data: List of user information dictionaries
        file_path: The path to save the CSV file
    """
    try:
        # Define CSV headers
        headers = [
            'Username', 'Email', 'Created At', 'Last Activity At', 
            'Last Active Editor', 'Editor Version', 'Plugin', 'Plugin Version',
            'Team Slug'
        ]

        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # Write data to CSV file
        with open(file_path, 'w', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=headers)
            writer.writeheader()
            writer.writerows(data)

        logging.info(f"Data saved to {file_path}")
    except Exception as e:
        logging.error(f"Error saving data to CSV: {str(e)}", exc_info=True)
        raise

def upload_to_blob_storage(connection_string, container_name, local_file_path, blob_name):
    """
    Upload a file to Azure Blob Storage.
    
    Parameters:
        connection_string: The Azure Storage connection string
        container_name: The name of the blob container
        local_file_path: The local path to the file to upload
        blob_name: The name to give the blob in storage
    """
    try:
        logging.info(f"Uploading {local_file_path} to Blob Storage container '{container_name}' as '{blob_name}'")
        
        # Create a BlobServiceClient using DefaultAzureCredential if no connection string
        if connection_string:
            blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        else:
            # Use DefaultAzureCredential with account URL when conn string not available
            account_name = os.environ.get("STORAGE_ACCOUNT_NAME")
            if not account_name:
                raise ValueError("STORAGE_ACCOUNT_NAME environment variable must be set when not using connection string")
            account_url = f"https://{account_name}.blob.core.windows.net"
            credential = DefaultAzureCredential()
            blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
            
        # Get or create container
        container_client = blob_service_client.get_container_client(container_name)
        try:
            container_client.create_container()
            logging.info(f"Created container: {container_name}")
        except ResourceExistsError:
            logging.info(f"Container '{container_name}' already exists")
        
        # Upload the file
        blob_client = container_client.get_blob_client(blob_name)
        with open(local_file_path, "rb") as data:
            blob_client.upload_blob(data, overwrite=True)
            
        logging.info(f"File uploaded to Blob Storage successfully: {blob_name}")
        
        # Return the blob URL for potential use
        return blob_client.url
    except Exception as e:
        logging.error(f"Error uploading to Blob Storage: {str(e)}", exc_info=True)
        raise

def get_email_recipients(connection_string, container_name, email_list_blob_path):
    """
    Get the email recipients list from Azure Blob Storage.
    
    Parameters:
        connection_string: The Azure Storage connection string
        container_name: The name of the blob container
        email_list_blob_path: The path to the blob containing the email list JSON
        
    Returns:
        List of email addresses
    """
    try:
        logging.info(f"Getting email recipients from Blob Storage: {email_list_blob_path}")
        
        # Create a BlobServiceClient
        if connection_string:
            blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        else:
            # Use DefaultAzureCredential with account URL when conn string not available
            account_name = os.environ.get("STORAGE_ACCOUNT_NAME")
            if not account_name:
                raise ValueError("STORAGE_ACCOUNT_NAME environment variable must be set when not using connection string")
            account_url = f"https://{account_name}.blob.core.windows.net"
            credential = DefaultAzureCredential()
            blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
            
        # Get container client
        container_client = blob_service_client.get_container_client(container_name)
        
        # Get blob client and download blob
        blob_client = container_client.get_blob_client(email_list_blob_path)
        download_stream = blob_client.download_blob()
        
        # Parse JSON content
        email_data = json.loads(download_stream.readall().decode('utf-8'))
        
        if 'emails' in email_data and isinstance(email_data['emails'], list):
            return email_data['emails']
        else:
            logging.warning("Email list JSON does not contain expected 'emails' array")
            return []
    except Exception as e:
        logging.error(f"Error getting email recipients: {str(e)}", exc_info=True)
        # Fall back to a default list or empty list in case of error
        return []

def send_email(connection_string, container_name, email_list_blob_path, 
               communication_service_connection_string, sender_email,
               report_file_path, report_filename):
    """
    Send email with the Copilot billing seats report attached.
    
    Parameters:
        connection_string: Azure Storage connection string
        container_name: Blob container name
        email_list_blob_path: Path to the blob with email recipients
        communication_service_connection_string: Azure Communication Services connection string
        sender_email: Email address to send from
        report_file_path: Local path to the report file
        report_filename: Filename of the report
    """
    try:
        logging.info("Preparing to send email report")
        
        # Get email recipients
        recipients = get_email_recipients(connection_string, container_name, email_list_blob_path)
        
        if not recipients:
            logging.error("No email recipients found. Cannot send report.")
            return
            
        logging.info(f"Sending email to {len(recipients)} recipients")
        
        # Create email client
        email_client = EmailClient.from_connection_string(communication_service_connection_string)
        
        # Read the report file
        with open(report_file_path, 'rb') as file:
            file_content = file.read()
            
        # Prepare email content
        subject = f"Copilot Report - {datetime.now().strftime('%Y-%m-%d')}"
        content = """
        <html>
        <body>
        <p>Hello,</p>
        <p>Please find the attached GitHub Copilot Report.</p>
        <p>Thanks,<br>CCOE Team</p>
        </body>
        </html>
        """
        
        # Send email to all recipients
        poller = email_client.begin_send(
            sender=sender_email,
            recipients_to=recipients,
            subject=subject,
            html_content=content,
            attachments=[
                {
                    "name": report_filename,
                    "content_type": "text/csv",
                    "content_bytes": file_content
                }
            ]
        )
        
        # Wait for the operation to complete
        result = poller.result()
        
        logging.info(f"Email sent successfully. Message ID: {result.message_id}")
        
    except Exception as e:
        logging.error(f"Error sending email: {str(e)}", exc_info=True)
        # In a production environment, we would want to alert on email failures
        # Here we could add application insights or other monitoring
