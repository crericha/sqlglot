SELECT z.a FROM x;
SELECT z.* FROM x;
SELECT x FROM x;
INSERT INTO x VALUES (1, 2);
SELECT a FROM x AS z JOIN y AS z;
SELECT a FROM x JOIN (SELECT b FROM y WHERE y.b = x.c);
SELECT a FROM x AS y JOIN (SELECT a FROM y) AS q ON y.a = q.a;
SELECT q.a FROM (SELECT x.b FROM x) AS z JOIN (SELECT a FROM z) AS q ON z.b = q.a;
SELECT b FROM x AS a CROSS JOIN y AS b CROSS JOIN y AS c;
SELECT x.a FROM x JOIN y USING (a);
SELECT a, SUM(b) FROM x GROUP BY 3;
SELECT p FROM (SELECT x from xx) y CROSS JOIN yy CROSS JOIN zz
select a from (select * from x cross join y);
