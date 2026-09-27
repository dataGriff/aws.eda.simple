-- Views over the archive. Loaded by `make duckdb`, `make query` and tests/test_archive_query.py.
--
-- The caller points this file at an archive first, so the same SQL serves S3, LocalStack
-- and a local directory:
--
--     SET VARIABLE archive = 's3://<bucket>/events/**/*.jsonl.gz';
--     .read queries/views.sql

-- One row per event on the bus.
--
-- Each archived line is the EventBridge envelope exactly as delivered, so this is where
-- the envelope's names become column names. `detail` - the inbound event, payload
-- included - is kept as JSON rather than expanded into a struct, so new payload fields
-- can never break a query that does not ask for them.
--
-- Delivery to the archive is at-least-once: a batch whose S3 write failed is redelivered,
-- so an event can appear in two files. QUALIFY keeps the first copy by envelope id.
CREATE OR REPLACE VIEW events AS
SELECT id                    AS bus_event_id,
       detail ->> '$.id'     AS event_id,
       "detail-type"         AS event_type,
       source,
       "time"                AS event_time,
       detail,
       dt,
       strptime(regexp_extract(filename, '(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})Z', 1),
                '%Y-%m-%dT%H-%M-%S') AS archived_at
FROM   read_json(getvariable('archive'),
                 format = 'newline_delimited',
                 hive_partitioning = true,
                 filename = true,
                 columns = {'id': 'VARCHAR', 'detail-type': 'VARCHAR', 'source': 'VARCHAR',
                            'time': 'TIMESTAMP', 'detail': 'JSON'})
QUALIFY row_number() OVER (PARTITION BY id ORDER BY filename) = 1;

-- One row per line item on an order.created event.
--
-- The items array has to be unnested at query time. Casting it to a typed struct once
-- means no per-field casting afterwards. The comma before unnest() is a lateral join: one
-- output row per array element, with the parent event's columns repeated.
--
-- Only order.created carries items, hence the filter - without it the other two event
-- types contribute nothing but are still scanned.
CREATE OR REPLACE VIEW order_items AS
SELECT e.event_id,
       e.event_time,
       json_extract_string(e.detail, '$.data.order_id') AS order_id,
       json_extract_string(e.detail, '$.data.currency') AS currency,
       i.sku,
       i.qty,
       i.unit_price,
       round(i.qty * i.unit_price, 2) AS line_total
FROM   events e,
       unnest(from_json(json_extract(e.detail, '$.data.items'),
                        '["STRUCT(sku VARCHAR, qty INTEGER, unit_price DOUBLE)"]')) AS t(i)
WHERE  e.event_type = 'order.created';

-- One row per order.created event, with the payload's own total.
CREATE OR REPLACE VIEW orders AS
SELECT event_id,
       event_time,
       json_extract_string(detail, '$.data.order_id')          AS order_id,
       json_extract_string(detail, '$.data.customer_name')     AS customer_name,
       json_extract_string(detail, '$.data.currency')          AS currency,
       CAST(json_extract_string(detail, '$.data.total') AS DOUBLE) AS total
FROM   events
WHERE  event_type = 'order.created';

-- No arrays in this payload, so no unnest: the same extraction pattern, flat.
CREATE OR REPLACE VIEW payments AS
SELECT event_id,
       event_time,
       json_extract_string(detail, '$.data.payment_id')         AS payment_id,
       json_extract_string(detail, '$.data.order_id')           AS order_id,
       json_extract_string(detail, '$.data.method')             AS method,
       json_extract_string(detail, '$.data.currency')           AS currency,
       CAST(json_extract_string(detail, '$.data.amount') AS DOUBLE) AS amount
FROM   events
WHERE  event_type = 'payment.received';
