-- The database schema as it was before uploads (schema version 1), with synthetic rows in every
-- table. Committed on purpose: migration 2 is tested against this, not against the current models.
-- Ids are not contiguous so that "ids are preserved" means something.

CREATE TABLE documents (
	id INTEGER NOT NULL, 
	paperless_id INTEGER NOT NULL, 
	title VARCHAR NOT NULL, 
	kind VARCHAR(12) NOT NULL, 
	doc_date DATE, 
	ignored BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_documents_paperless_id ON documents (paperless_id);
CREATE INDEX ix_documents_ignored ON documents (ignored);

CREATE TABLE app_settings (
	"key" VARCHAR NOT NULL, 
	value VARCHAR NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY ("key")
);

CREATE TABLE extraction_runs (
	id INTEGER NOT NULL, 
	document_id INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	duration_s FLOAT, 
	pages INTEGER NOT NULL, 
	page_errors VARCHAR NOT NULL, 
	error VARCHAR NOT NULL, 
	settings VARCHAR NOT NULL, 
	verified INTEGER NOT NULL, 
	needs_review INTEGER NOT NULL, 
	kept_approved INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);
CREATE INDEX ix_extraction_runs_document_id ON extraction_runs (document_id);

CREATE TABLE extracted_values (
	id INTEGER NOT NULL, 
	document_id INTEGER NOT NULL, 
	run_id INTEGER, 
	test_code VARCHAR, 
	raw_name VARCHAR NOT NULL, 
	value_text VARCHAR NOT NULL, 
	value_num FLOAT, 
	unit VARCHAR NOT NULL, 
	ref_range VARCHAR NOT NULL, 
	flag VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	reason VARCHAR NOT NULL, 
	page INTEGER, 
	reader_a VARCHAR, 
	reader_b VARCHAR, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE, 
	FOREIGN KEY(run_id) REFERENCES extraction_runs (id) ON DELETE SET NULL
);
CREATE INDEX ix_extracted_values_document_id ON extracted_values (document_id);
CREATE INDEX ix_extracted_values_test_code ON extracted_values (test_code);
CREATE UNIQUE INDEX ux_extracted_values_approved ON extracted_values (document_id, test_code) WHERE status = 'approved';
CREATE INDEX ix_extracted_values_status ON extracted_values (status);

INSERT INTO documents VALUES
  (1, 501, 'Synthetic blood test', 'BLOOD_TEST', '2025-03-01', 0, '2025-03-02 10:00:00', '2025-03-02 10:00:00'),
  (5, 502, 'Synthetic report',     'REPORT',     NULL,         1, '2025-04-02 10:00:00', '2025-04-02 10:00:00'),
  (9, 777, 'Synthetic other',      'OTHER',      '2025-05-05', 0, '2025-05-06 10:00:00', '2025-05-06 10:00:00');

INSERT INTO extraction_runs VALUES
  (2, 1, 'done',  '2025-03-03 09:00:00', '2025-03-03 09:01:00', 60.0, 1, '[]', '', '{}', 2, 1, 0),
  (7, 9, 'error', '2025-05-07 09:00:00', '2025-05-07 09:00:05', 5.0,  0, '[]', 'synthetic failure', '{}', 0, 0, 0);

INSERT INTO extracted_values VALUES
  (1, 1, 2,    'HGB', 'Hemoglobin', '13.5', 13.5, 'g/dL', '12-16', '',  'approved',     '', 1, 'a', 'b', '2025-03-03 09:01:00', '2025-03-03 09:01:00'),
  (2, 1, 2,    'WBC', 'WBC',        '11.2', 11.2, '10^9/L', '4-10', 'H', 'verified',     '', 1, 'a', 'b', '2025-03-03 09:01:00', '2025-03-03 09:01:00'),
  (3, 1, 2,    NULL,  'Unknown',    '1',    1.0,  '',     '',      '',  'needs_review', 'no match', 1, 'a', NULL, '2025-03-03 09:01:00', '2025-03-03 09:01:00'),
  (4, 1, NULL, 'PLT', 'Platelets',  '250',  250.0, '10^9/L', '150-400', '', 'rejected',  '', 1, NULL, 'b', '2025-03-03 09:01:00', '2025-03-03 09:01:00'),
  (5, 5, NULL, 'HGB', 'Hemoglobin', '12.0', 12.0, 'g/dL', '12-16', '',  'approved',     '', NULL, NULL, NULL, '2025-04-03 09:01:00', '2025-04-03 09:01:00'),
  (6, 9, 7,    'GLU', 'Glucose',    '90',   90.0, 'mg/dL', '70-100', '', 'verified',     '', 2, 'a', 'b', '2025-05-07 09:00:05', '2025-05-07 09:00:05');

INSERT INTO app_settings VALUES
  ('general.language', '"el"', '2025-06-01 12:00:00'),
  ('uploads.max_file_mb', '20', '2025-06-01 12:00:00');
