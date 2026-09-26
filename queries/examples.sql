-- Example queries. Run them all with `make query`, or pick one at the `make duckdb` prompt.
-- The views these use are defined in queries/views.sql.

-- What is in the table at all.
SELECT event_type, count(*) AS events, min(event_time) AS first_seen, max(event_time) AS last_seen
FROM   events
GROUP  BY 1
ORDER  BY 2 DESC;

-- How long events take to land: Firehose buffers for 60s, so expect roughly that.
SELECT round(avg(epoch(ingest_time) - epoch(event_time)), 1) AS avg_lag_seconds,
       max(epoch(ingest_time) - epoch(event_time))           AS worst_lag_seconds
FROM   events;

-- Individual line items, newest first.
SELECT event_time, order_id, sku, qty, unit_price, line_total
FROM   order_items
ORDER  BY event_time DESC
LIMIT  10;

-- Best selling SKUs.
SELECT sku, sum(qty) AS units, round(sum(line_total), 2) AS revenue
FROM   order_items
GROUP  BY 1
ORDER  BY 3 DESC
LIMIT  10;

-- Revenue per currency, from the order header rather than the items.
SELECT currency, count(*) AS orders, round(sum(total), 2) AS revenue
FROM   orders
GROUP  BY 1
ORDER  BY 3 DESC;

-- Does the item maths agree with the total in the same payload? It should, for every order.
SELECT count(*)                                                          AS orders_checked,
       sum(CASE WHEN abs(items_total - o.total) < 0.01 THEN 1 ELSE 0 END) AS reconciled
FROM   orders o
JOIN  (SELECT order_id, round(sum(line_total), 2) AS items_total
       FROM   order_items GROUP BY 1) i USING (order_id);

-- Orders that have been paid, joining two event types through the payload.
SELECT o.order_id, o.total, p.method, round(p.amount, 2) AS paid
FROM   orders o
JOIN   payments p USING (order_id)
ORDER  BY o.event_time DESC
LIMIT  10;
