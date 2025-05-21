import json
import boto3
import os
import requests
import csv
import logging
import time
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart

ENTERPRISE_SLUG = "ybwjt6t"
AUTH_TOKEN = ""
BUCKET_NAME = 'ibs-copilot-reports'
today = time.strftime("%Y_%m_%d")

def lambda_handler(event, context):

    # Setup logging
    logging.basicConfig(level=logging.INFO, filename='github_copilot_billing.log',
    	format='%(asctime)s - %(levelname)s - %(message)s')

    # Fetch from environment variables
    teams = fetch_teams(ENTERPRISE_SLUG, AUTH_TOKEN)
    seats_info = get_copilot_billing_seats(teams)
    if isinstance(seats_info, str):  # If there's an error message, print it
        logging.error(seats_info)
    else:
        save_to_csv(seats_info)
        logging.info(f"Number of users fetched: {len(seats_info)}")
        send_email()


def fetch_teams(enterprise_slug, token):
    """Fetches all teams in the enterprise with pagination handling."""
    url = f"https://api.github.com/enterprises/{enterprise_slug}/teams?per_page=100"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    teams = []
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            teams.extend([{'id': team['id'], 'name': team['name']} for team in data])
            url = response.links.get('next', {}).get('url')  # Handle pagination
            if url:
                logging.info("Fetching next page of teams...")
        else:
            logging.error(f"Failed to fetch teams with status code {response.status_code}. Error: {response.text}")
            break  # Stop the loop if there's an error
    logging.info(f"Fetched {len(teams)} teams successfully.")
    return teams

def get_user_details(username):
    """Fetches details like email and created_at for a given GitHub username."""
    user_api_url = f"https://api.github.com/users/{username}"
    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}"
    }
    response = requests.get(user_api_url, headers=headers)
    
    if response.status_code == 200:
        user_data = response.json()
        email = user_data.get('email') or 'N/A'
        created_at = user_data.get('created_at') or 'N/A'
        return email, created_at
    else:
        logging.error(f"Failed to fetch details for {username}: {response.status_code} - {response.text}")
        return 'N/A', 'N/A'

def check_rate_limit(headers):
    """Check and handle GitHub API rate limiting."""
    remaining = int(headers.get('X-RateLimit-Remaining', 0))
    reset_time = int(headers.get('X-RateLimit-Reset', 0))
    
    if remaining == 0:
        wait_time = max(reset_time - time.time(), 0)
        logging.warning(f"Rate limit reached. Waiting for {wait_time} seconds.")
        time.sleep(wait_time + 1)  # Wait until the reset time plus a buffer

def get_copilot_billing_seats(teams):
    """Fetches the Copilot billing seats data and processes it for each team."""
    api_url = f"https://api.github.com/enterprises/{ENTERPRISE_SLUG}/copilot/billing/seats"
    headers = {
        "Authorization": f"Bearer {AUTH_TOKEN}"
    }

    users_info = []
    page = 1
    
    while True:
        logging.info(f"Fetching page {page} of Copilot billing seats.")
        
        response = requests.get(api_url, headers=headers, params={'page': page})
        check_rate_limit(response.headers)
        
        if response.status_code == 200:
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
                    email, created_at = get_user_details(username)
                    
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
            break

    return users_info

def save_to_csv(data, filename='copilot_billing_seats.csv'):
    # Define CSV headers
    headers = [
        'Username', 'Email', 'Created At', 'Last Activity At', 
        'Last Active Editor', 'Editor Version', 'Plugin', 'Plugin Version',
        'Team Slug'
    ]

    # Write data to CSV file
    with open('/tmp/' + filename, 'w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data)

    logging.info(f"Data saved to {filename}")

    s3 = boto3.resource('s3')
    s3file = 'copilot_billing_seats_'+ today +'.csv'
    s3.Bucket(BUCKET_NAME).upload_file('/tmp/' + filename, s3file)

    logging.info(f"Data saved to s3 file {s3file}")

def send_email(report_filename='copilot_billing_seats.csv'):
    client = boto3.client("ses")
    f = open('emails.json')
    data = json.load(f)
    message = MIMEMultipart()
    message['Subject'] = 'Copilot Report'
    message['From'] = 'copilot_report@ibsplc.org'
    #message['To'] = ', '.join(['jaseem.jabbar@ibsplc.com','romy.thomas@ibsplc.com'])
    message['To'] = ', '.join(data['emails'])
    # message body
    part = MIMEText('Hello, <br/><br/>Please find the attached Copilot Report<br/><br/>Thanks,<br/>CCOE', 'html')
    message.attach(part)
    part = MIMEApplication(open('/tmp/' + report_filename, 'rb').read())
    part.add_header('Content-Disposition', 'attachment', filename=report_filename)
    message.attach(part)
    response = client.send_raw_email(
        Source=message['From'],
        #Destinations=['jaseem.jabbar@ibsplc.com','romy.thomas@ibsplc.com'],
        Destinations=data['emails'],
        RawMessage={
            'Data': message.as_string()
        }
    )