CREATE DATABASE superset;
CREATE ROLE totemtang WITH SUPERUSER LOGIN PASSWORD '1234';
GRANT ALL PRIVILEGES ON DATABASE superset TO totemtang;