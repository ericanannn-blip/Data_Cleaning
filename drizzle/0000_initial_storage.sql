CREATE TABLE `batch_items` (
	`sequence` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`batch_id` text NOT NULL,
	`file_id` text,
	`rejection` text,
	FOREIGN KEY (`batch_id`) REFERENCES `batches`(`id`) ON UPDATE no action ON DELETE no action,
	FOREIGN KEY (`file_id`) REFERENCES `files`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE INDEX `idx_batch_sequence` ON `batch_items` (`batch_id`,`sequence`);--> statement-breakpoint
CREATE TABLE `batches` (
	`id` text PRIMARY KEY NOT NULL,
	`created_at` text NOT NULL,
	`state` text DEFAULT 'open' NOT NULL
);
--> statement-breakpoint
CREATE TABLE `files` (
	`id` text PRIMARY KEY NOT NULL,
	`original_name` text NOT NULL,
	`extension` text NOT NULL,
	`file_size` integer NOT NULL,
	`mime_type` text NOT NULL,
	`encoding` text,
	`magic_bytes` text NOT NULL,
	`printable_ratio` real NOT NULL,
	`sample_bytes` integer NOT NULL,
	`detected_type` text NOT NULL,
	`confidence` text NOT NULL,
	`status` text NOT NULL,
	`created_at` text NOT NULL,
	`error_message` text,
	`sha256` text NOT NULL,
	`r2_key` text NOT NULL,
	`analysis_json` text
);
--> statement-breakpoint
CREATE INDEX `idx_files_created_at` ON `files` (`created_at`,`id`);--> statement-breakpoint
CREATE INDEX `idx_files_sha256` ON `files` (`sha256`);