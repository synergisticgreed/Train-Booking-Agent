-- Minimal test data for Phase 2 tools. Dates assume tests run with as_of in Nov/Dec 2026.
-- 2026-12-03 = Thu, 2026-12-04 = Fri, 2026-12-05 = Sat.

INSERT INTO stations VALUES
 ('PUNE', 'Pune Junction', 'Poona', 'Pune'),
 ('NGP',  'Nagpur Junction', NULL, 'Nagpur'),
 ('CSMT', 'Mumbai CSMT', 'Bombay,CST,Mumbai CST,Bombay VT', 'Mumbai'),
 ('LTT',  'Lokmanya Tilak Terminus', 'Kurla', 'Mumbai'),
 ('KYN',  'Kalyan Junction', NULL, 'Kalyan'),
 ('NDLS', 'New Delhi', 'Delhi', 'Delhi'),
 ('DLI',  'Old Delhi', 'Delhi Junction', 'Delhi');

INSERT INTO trains VALUES
 ('12123', 'Vidarbha Express', 'Daily', 'SL,3A,2A'),
 ('11001', 'Deccan Day Express', 'Daily', 'SL,CC'),
 ('22111', 'Night Owl Express', 'Thu', 'SL,3A');

INSERT INTO train_stops VALUES
 ('12123', 'CSMT', 1, NULL,    '15:00', 0),
 ('12123', 'KYN',  2, '15:48', '15:50', 0),
 ('12123', 'PUNE', 3, '18:25', '18:30', 0),
 ('12123', 'NGP',  4, '06:10', NULL,    1),
 ('11001', 'PUNE', 1, NULL,    '07:00', 0),
 ('11001', 'NGP',  2, '20:00', NULL,    0),
 ('22111', 'LTT',  1, NULL,    '21:10', 0),
 ('22111', 'PUNE', 2, '00:20', '00:30', 1),
 ('22111', 'NGP',  3, '13:00', NULL,    1);

INSERT INTO fares VALUES
 ('12123', '3A', 'PUNE', 'NGP', 1250), ('12123', '2A', 'PUNE', 'NGP', 2040),
 ('12123', 'SL', 'PUNE', 'NGP', 480),  ('12123', '3A', 'CSMT', 'NGP', 1600),
 ('12123', '2A', 'CSMT', 'NGP', 2400), ('22111', '3A', 'PUNE', 'NGP', 1200),
 ('11001', 'SL', 'PUNE', 'NGP', 400);

INSERT INTO seats VALUES
 ('12123', '2026-12-05', '3A', 4, 0, 0),
 ('12123', '2026-12-05', '2A', 0, 2, 0),
 ('12123', '2026-12-05', 'SL', 0, 0, 15),
 ('22111', '2026-12-03', '3A', 10, 0, 0);

INSERT INTO passengers VALUES
 ('P001', 'Asha Verma', '1234', 'en'),
 ('P002', 'Ravi Kumar', '5678', 'hinglish'),
 ('P003', 'Meera Iyer', '9012', 'en');

INSERT INTO bookings VALUES
 ('UR-30418', 'P001', '12123', '2026-12-05', 'PUNE', 'NGP', '2A', 2040, 'CONFIRMED', '2026-11-01T10:00:00+05:30'),
 ('UR-30500', 'P001', '11001', '2026-11-10', 'PUNE', 'NGP', 'SL', 400,  'CANCELLED', '2026-11-01T10:05:00+05:30'),
 ('UR-48213', 'P002', '12123', '2026-12-05', 'PUNE', 'NGP', '3A', 2500, 'CONFIRMED', '2026-11-02T09:00:00+05:30');

INSERT INTO booking_travellers VALUES
 ('UR-30418', 'Asha Verma', 34, 'CNF', 'A1', '12'),
 ('UR-48213', 'Ravi Kumar', 41, 'CNF', 'B2', '31'),
 ('UR-48213', 'Sita Kumar', 38, 'WL',  NULL, NULL);

INSERT INTO settings (name, value, source_doc, effective_from) VALUES
 ('cancel_tier1_hours',         '48',  'RUL-CAN-01#3. Refund table', '1900-01-01'),
 ('cancel_tier2_hours',         '12',  'RUL-CAN-01#3. Refund table', '1900-01-01'),
 ('cancel_no_refund_hours',     '4',   'RUL-CAN-01#3. Refund table', '1900-01-01'),
 ('cancel_tier2_pct',           '25',  'RUL-CAN-01#3. Refund table', '1900-01-01'),
 ('cancel_tier3_pct',           '50',  'RUL-CAN-01#3. Refund table', '1900-01-01'),
 ('cancel_flat_charge_SL',      '120', 'RUL-CAN-01#3. Refund table', '1900-01-01'),
 ('cancel_flat_charge_3A',      '180', 'RUL-CAN-01#3. Refund table', '1900-01-01'),
 ('cancel_flat_charge_2A',      '240', 'RUL-CAN-01#3. Refund table', '1900-01-01'),
 ('cancel_flat_charge_2A',      '200', 'CIR-2026-07#Revised charges', '2026-11-01'),
 ('wl_clerkage_charge',         '60',  'RUL-CAN-01#5. Waitlisted tickets', '1900-01-01'),
 ('train_cancelled_refund_pct', '100', 'RUL-CAN-01#6. Train cancelled', '1900-01-01');

INSERT INTO settings (name, value, source_doc, effective_from) VALUES
 ('action_ttl_minutes',           '15', 'RUL-BKG-01#7. Confirming changes', '1900-01-01'),
 ('boarding_change_cutoff_hours', '24', 'RUL-BRD-01#2. Cut-off',            '1900-01-01');

-- Operator-cancelled train (refund complaint case) for P002.
INSERT INTO bookings VALUES
 ('UR-41000', 'P002', '22111', '2026-11-12', 'LTT', 'NGP', '3A', 1500, 'TRAIN_CANCELLED', '2026-10-30T12:00:00+05:30');
INSERT INTO booking_travellers VALUES ('UR-41000', 'Ravi Kumar', 41, 'CNF', 'B1', '7');
