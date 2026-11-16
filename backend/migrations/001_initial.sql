-- MySQL 8 actual DDL; generated after acceptance, seed counters removed.
CREATE TABLE `audits` (
  `id` int NOT NULL AUTO_INCREMENT,
  `actor` varchar(100) DEFAULT NULL,
  `action` varchar(200) DEFAULT NULL,
  `created` varchar(40) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `cohorts` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(100) DEFAULT NULL,
  `start_week` varchar(10) DEFAULT NULL,
  `end_week` varchar(10) DEFAULT NULL,
  `as_of` varchar(10) DEFAULT NULL,
  `horizon` int DEFAULT NULL,
  `state` varchar(20) DEFAULT NULL,
  `result` json DEFAULT NULL,
  `error` text,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `events` (
  `id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(60) NOT NULL,
  `learner` varchar(60) NOT NULL,
  `occurred_at` varchar(40) NOT NULL,
  `course` varchar(40) DEFAULT NULL,
  `note` text,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`),
  KEY `ix_events_learner` (`learner`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `email` varchar(100) NOT NULL,
  `password` varchar(200) DEFAULT NULL,
  `role` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `archive_jobs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `cutoff` varchar(10) NOT NULL,
  `state` varchar(20) DEFAULT NULL,
  `candidate_count` int DEFAULT NULL,
  `archived_count` int DEFAULT NULL,
  `batch_checksum` varchar(64) DEFAULT NULL,
  `error` text,
  `created_at` varchar(40) DEFAULT NULL,
  `started_at` varchar(40) DEFAULT NULL,
  `finished_at` varchar(40) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_archive_jobs_cutoff` (`cutoff`),
  KEY `ix_archive_jobs_state` (`state`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `archived_events` (
  `id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(60) NOT NULL,
  `learner` varchar(60) NOT NULL,
  `occurred_at` varchar(40) NOT NULL,
  `course` varchar(40) DEFAULT NULL,
  `note` text,
  `origin_event_id` int DEFAULT NULL,
  `archive_job_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `code` (`code`),
  KEY `ix_archived_events_learner` (`learner`),
  KEY `ix_archived_events_origin_event_id` (`origin_event_id`),
  KEY `ix_archived_events_archive_job_id` (`archive_job_id`),
  CONSTRAINT `archived_events_ibfk_1` FOREIGN KEY (`archive_job_id`) REFERENCES `archive_jobs` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `event_locks` (
  `id` int NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
INSERT INTO `event_locks` (`id`) VALUES (1);

CREATE TABLE `interventions` (
  `id` int NOT NULL AUTO_INCREMENT,
  `cohort_id` int DEFAULT NULL,
  `cohort_week` varchar(10) DEFAULT NULL,
  `week_index` int DEFAULT NULL,
  `note` text,
  `members` json DEFAULT NULL,
  `status` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `cohort_id` (`cohort_id`),
  CONSTRAINT `interventions_ibfk_1` FOREIGN KEY (`cohort_id`) REFERENCES `cohorts` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

