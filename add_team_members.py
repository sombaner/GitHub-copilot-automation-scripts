import requests
import time

def add_team_member(username, api_token):
    # API endpoint
    base_url = "https://api.github.com/orgs/<orgname>/teams/<teamname>/memberships/"
    url = f"{base_url}{username}"

    # Headers
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {api_token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # Request body
    data = {
        "role": "maintainer"
    }

    try:
        response = requests.put(url, headers=headers, json=data)
        print(f"Adding {username}: {response.status_code}")
        if response.status_code != 200:
            print(f"Error: {response.json()}")
        time.sleep(1)  # Rate limiting - wait 1 second between requests
    except Exception as e:
        print(f"Error adding {username}: {str(e)}")

def main():
    # Replace with your GitHub API token
    api_token = "put the token here"

    # Read handles from file
    with open('handles.txt', 'r') as file:
        handles = [line.strip() for line in file if line.strip()]

    # Process each handle
    for handle in handles:
        print(f"Processing handle: {handle}")
        add_team_member(handle, api_token)

if __name__ == "__main__":
    main()