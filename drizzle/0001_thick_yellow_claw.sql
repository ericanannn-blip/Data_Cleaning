CREATE TABLE `upload_parts` (
	`upload_id` text NOT NULL,
	`part_number` integer NOT NULL,
	`size` integer NOT NULL,
	`sha256` text NOT NULL,
	`etag` text,
	PRIMARY KEY(`upload_id`, `part_number`),
	FOREIGN KEY (`upload_id`) REFERENCES `upload_sessions`(`id`) ON UPDATE no action ON DELETE no action
);
--> statement-breakpoint
CREATE TABLE `upload_sessions` (
	`id` text PRIMARY KEY NOT NULL,
	`r2_key` text NOT NULL,
	`multipart_id` text NOT NULL,
	`source_name` text NOT NULL,
	`mapped_name` text,
	`size` integer NOT NULL,
	`batch_id` text,
	`state` text DEFAULT 'open' NOT NULL,
	`created_at` text NOT NULL,
	`updated_at` text NOT NULL,
	FOREIGN KEY (`batch_id`) REFERENCES `batches`(`id`) ON UPDATE no action ON DELETE no action
);
