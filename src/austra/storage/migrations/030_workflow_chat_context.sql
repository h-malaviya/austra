-- A workflow conversation belongs to the same owner and tenant as an ordinary chat.
ALTER TABLE organization_chats ADD COLUMN workflow_context jsonb;
