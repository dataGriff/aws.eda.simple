-- Views over the `events` table, loaded automatically by `make duckdb` and `make query`.
--
-- `events` itself is created by the Makefile, which knows the attached catalog name.
-- Everything here builds on it, and only needs re-running if you change these definitions.

-- One row per line item on an order.created event.
--
-- `detail` holds the whole inbound event as a JSON string, so the items array has to be
-- unnested at query time. Casting it to a typed struct once means no per-field casting
-- afterwards. The comma before unnest() is a lateral join: one output row per array
-- element, with the parent event's columns repeated.
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
