""" 
Script for importing a 4chan posts csv generated by 4CAT itself.
Useful to import datasets from other 4CAT instances.

psql command to export and compress a csv from 4CAT:
psql -d fourcat -c "COPY (SELECT * FROM posts_4chan WHERE board='BOARD') TO stdout WITH HEADER CSV DELIMITER ',';" | gzip > /path/file.csv.gz
"""

import argparse
import json
import time
import csv
import sys
import os
import re

from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)) + "/..")
from common.lib.database import Database
from common.lib.logger import Logger

# parse parameters
cli = argparse.ArgumentParser()
cli.add_argument("-i", "--input", required=True, help="File to read from, containing a CSV dump")
cli.add_argument("-d", "--datasource", type=str, required=True, help="Datasource ID")
cli.add_argument("-b", "--board", type=str, required=True, help="Board name")
cli.add_argument("-s", "--skip_duplicates", type=str, required=True, help="If duplicate posts should be skipped (useful if there's already data in the table)")
cli.add_argument("-o", "--offset", type=int, required=False, help="How many rows to skip")
args = cli.parse_args()

if not Path(args.input).exists() or not Path(args.input).is_file():
	print("%s is not a valid folder name." % args.input)
	sys.exit(1)

logger = Logger()
db = Database(logger=logger, appname="queue-dump")

csvnone = re.compile(r"^N$")

safe = False
if args.skip_duplicates.lower() == "true":
	print("Skipping duplicate rows (ON CONFLICT DO NOTHING).")
	safe = True

with open(args.input, encoding="utf-8") as inputfile:

	reader = csv.DictReader(inputfile)
	fieldnames = reader.fieldnames
	
	# Skip headers
	next(reader, None)

	posts = 0
	threads_added = 0
	posts_added = 0
	dups = 0
	threads = {}

	# We keep count of what threads we have last encounterd.
	# This is done to prevent RAM hogging: we're inserting threads
	# we haven't seen in a while to the db and removing them from this dict. 
	threads_last_seen = {}

	# Show status
	if args.offset:
		print("Skipping %s rows." % args.offset)

	for post in reader:

		posts += 1

		# Skip rows if needed. Can be useful when importing didn't go correctly.
		if args.offset and posts < args.offset:
			if posts % 1000000 == 0:
				print("Skipped %i posts." % posts)
			continue

		# We collect thread data first
		if post["thread_id"] not in threads:
			threads_added += 1
			threads[post["thread_id"]] = {
				"id": post["thread_id"],
				"board": args.board,
				"timestamp": 0,
				"timestamp_scraped": int(time.time()),
				"timestamp_modified": 0,
				"num_unique_ips": -1,
				"num_images": 0,
				"num_replies": 0,
				"limit_bump": False,
				"limit_image": False,
				"is_sticky": False,
				"is_closed": False,
				"post_last": 0
			}

		if post["thread_id"] == post["id"]:
			threads[post["thread_id"]]["timestamp"] = post["timestamp"]
			threads[post["thread_id"]]["is_sticky"] = False
			threads[post["thread_id"]]["is_closed"] = False

		if post["image_file"]:
			threads[post["thread_id"]]["num_images"] += 1

		threads[post["thread_id"]]["num_replies"] += 1
		threads[post["thread_id"]]["post_last"] = post["id"]
		threads[post["thread_id"]]["timestamp_modified"] = post["timestamp"]

		# We reset the count of when we last seen this thread to 1
		# to prevent committing incomplete thread data.
		# Increase the count for the other threads.
		threads_last_seen[post["thread_id"]] = 0
		for k, v in threads_last_seen.items():
			threads_last_seen[k] += 1

		# Collect post data
		post = {k: csvnone.sub("", post[k]) if post[k] else None for k in post}

		post_data = {
			"id": post["id"],
			"board": args.board,
			"thread_id": post["thread_id"],
			"timestamp": post.get("timestamp", ""),
			"subject": post.get("subject", ""),
			"body": post.get("body", ""),
			"author": post.get("author", ""),
			"author_type": post.get("author_type", ""),
			"author_type_id": post.get("author_type_id", "N"),
			"author_trip": post.get("author_trip", ""),
			"country_code": post.get("country_code", ""),
			"country_name": post.get("country_name", ""),
			"image_file": post.get("image_file", ""),
			"image_url": post.get("image_url", ""),
			"image_4chan": post.get("image_4chan", ""),
			"image_md5": post.get("image_md5", ""),
			"image_dimensions": post.get("image_dimensions", ""),
			"image_filesize": post.get("image_filesize", 0),
			"semantic_url": post.get("semantic_url", ""),
			"unsorted_data": post.get("unsorted_data", "")
		}

		if post_data["image_md5"]:
			print(post_data)

		new_post = db.insert("posts_4chan", post_data, commit=False, safe=safe, constraints=["id", "board"])

		posts_added += new_post

		# Insert per every 10000 posts
		if posts % 10000 == 0:

			print("Committing posts %i - %i. %i new posts added. " % (posts - 10000, posts, posts_added), end="")
			
			# We're commiting the threads we didn't encounter in the last 100.000 posts. We're assuming they're complete and won't be seen in this archive anymore.
			# This is semi-necessary to prevent RAM hogging.
			threads_committed = 0
			for thread_seen, last_seen in threads_last_seen.items():
				if last_seen > 10000:
					db.insert("threads_4chan", data=threads[thread_seen], commit=False, safe=safe, constraints=["id", "board"])
					threads.pop(thread_seen)
					threads_committed += 1

			# Remove committed threads from the last seen list
			threads_last_seen = {k: v for k, v in threads_last_seen.items() if v < 10000}

			print("Committing threads %i - %i (%i still updating)." % (threads_added - threads_committed, threads_added, len(threads)))
			
			db.commit()

	# Add the last posts and threads as well
	print("Commiting leftover threads")
	for thread in threads.values():
		db.insert("threads_4chan", data=thread, commit=False, safe=safe, constraints=["id", "board"])

	db.commit()

print("Done")