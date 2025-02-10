# add-members-organization-team

This program adds bulk of unaffiliated members to organization and teams for an EMU account. Currently in GutHub UI we cannot added bulk members to an organization and the admin has to select each menber across all the pages and then add to the organization. If the enterprise has 5000 unaffiliated members, then the admin has to manually select the members one after the other to add it within organization. This progrma helps to automate the adding of members to organization and teams. If the account is EMU account, then only the members flowing through enterprise IDP can be added to the organizatiuon and team. 

## How to run the program 

1. Download the handles as csv  from the Enterprise --> people section.
2. Create a handles.txt only with the handles/shortcode
3. Add the respective base url for adding member to organization and teams - https://api.github.com/orgs/<orgname>/memberships/  &  https://api.github.com/orgs/<orgname>/teams/<teamname>/memberships/
4. Provide the api-token (the api token preferrably should have enterprise:admin access.
5. Create a team within the organization (Assuming  the organization is already created after the SSO integration) 
6. Execute the python script for adding organization members - python add_organization_members.py
7. Execute the python script for adding team members - python add_team_members.py
