-- Read-only export of the published public icon snapshot.
-- Save rows as JSON, then import with the privately configured entity-minis base URL.
select
  a.target_id as id,
  coalesce(e.name, s.name, r.name, c.name, a.alt_text, a.target_id) as name,
  coalesce(e.slug, s.slug, r.slug, c.slug) as seo_slug,
  coalesce(e.aliases, s.aliases, r.aliases, c.aliases, array[]::text[]) as aliases,
  split_part(a.object_path, '/', 1) as category,
  o.name as upstream_path,
  o.metadata->>'eTag' as source_etag,
  (o.metadata->>'size')::integer as source_bytes,
  a.source_kind, a.source_url
from public.assets a
join storage.objects o on o.bucket_id = 'entity-minis'
  and o.name = '48/' || a.object_path || '.webp'
left join public.entities e on a.target_type = 'entity' and e.id = a.target_id
left join public.standards s on a.target_type = 'standard' and s.id = a.target_id
left join public.regulations r on a.target_type = 'regulation' and r.id = a.target_id
left join public.card_records c on a.target_type = 'card' and c.id = a.target_id
where a.bucket_id = 'entity-assets'
  and a.is_current
  and a.status = 'published'
  and ((a.target_type = 'entity' and e.status = 'published')
    or (a.target_type = 'card' and c.status = 'published'))
order by a.object_path;
