# Austra project instructions

Follow the Bitontree coding standards supplied for this workspace. This file adds a project-specific scope rule.

When a user asks to improve Austra, implement and test Austra's capability. Do not manually carry out the example business task in Austra's live workspace. Treat supplied business files and chats as reproduction evidence; use isolated temporary workspaces for tests. Change live business records, schemas, chats, or approved rules only when the user separately and explicitly asks you to operate Austra on that data.

After every application code change, rebuild and restart the app container with `docker compose up -d --build app`, then verify that `docker compose ps app` reports a healthy service. Do this after tests and before reporting the change as complete. If the build or health check fails, report the failure and do not claim the running app has the change.
