ALTER TABLE conversation_memory_summaries
    ADD COLUMN IF NOT EXISTS working_content text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS compacted_at timestamptz,
    ADD COLUMN IF NOT EXISTS compacted_message_count integer NOT NULL DEFAULT 0;
