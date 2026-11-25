-- Repair blank administrator names only when a completed tour contains an explicit introduction.
WITH introductions AS (
    SELECT DISTINCT ON (message.organization_id)
        message.organization_id,
        (regexp_match(
            message.body,
            '([Ii]''m|[Ii]’m|[Mm]y name is|[Ii] am)[[:space:]]+([A-Z][a-z]{1,30})'
        ))[2] AS first_name
    FROM onboarding_messages AS message
    JOIN organization_tours AS tour ON tour.organization_id = message.organization_id
    WHERE message.role = 'user' AND tour.completed_at IS NOT NULL
      AND regexp_match(
          message.body,
          '([Ii]''m|[Ii]’m|[Mm]y name is|[Ii] am)[[:space:]]+([A-Z][a-z]{1,30})'
      ) IS NOT NULL
    ORDER BY message.organization_id, message.position
)
UPDATE users AS administrator SET full_name = introductions.first_name
FROM introductions
WHERE administrator.organization_id = introductions.organization_id
  AND administrator.role = 'org_admin' AND administrator.full_name IS NULL
  AND introductions.first_name NOT IN
      ('The', 'A', 'An', 'Responsible', 'Working', 'Here', 'From', 'With');
