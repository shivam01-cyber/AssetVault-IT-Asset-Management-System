CREATE DATABASE IF NOT EXISTS itam_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'itam_user'@'localhost' IDENTIFIED BY 'itam_pass_2026';
GRANT ALL PRIVILEGES ON itam_db.* TO 'itam_user'@'localhost';
FLUSH PRIVILEGES;
