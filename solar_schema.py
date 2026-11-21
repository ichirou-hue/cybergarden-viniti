"""Solar CSV columns and vocabularies from the participant template."""


TABLES = {
    "publications": "publication_id title year DOI authors in_scope exclusion_reason",
    "materials": "publication_id material_id material_name_raw formula_normalized material_class dimensionality additives bandgap_value bandgap_unit bandgap_method note",
    "devices": "publication_id device_id absorber_material_id device_label device_role device_type architecture substrate_type active_area area_unit area_definition encapsulation note",
    "layers": "publication_id layer_id device_id layer_order layer_function layer_material layer_thickness thickness_unit deposition_method annealing_temperature annealing_time atmosphere note",
    "measurements": "publication_id measurement_id device_id measurement_type spectrum light_intensity intensity_unit scan_direction scan_rate mask_used certified certification_lab temperature stability_protocol stress_conditions note",
    "results": "publication_id result_id device_id measurement_id material_id metric_name metric_value metric_unit aggregation spread_value n_devices time_point time_unit raw_value raw_unit normalized_value normalized_unit result_origin cited_publication_id source_page source_section source_table source_figure source_fragment extraction_status duplicate_of note",
    "issues": "publication_id issue_id record_ids check_name description source_page source_fragment",
}
TABLES = {table: columns.split() for table, columns in TABLES.items()}
KEYS = {
    "publications": "publication_id", "materials": "material_id", "devices": "device_id",
    "layers": "layer_id", "measurements": "measurement_id", "results": "result_id",
    "issues": "issue_id",
}
VOCAB = {
    ("publications", "in_scope"): {"true", "false"},
    ("materials", "material_class"): {"perovskite", "CIGS", "CdTe", "kesterite", "Sb2Se3", "thin-film Si", "other"},
    ("materials", "dimensionality"): {"3D", "2D", "2D-3D"},
    ("materials", "bandgap_method"): {"Tauc", "EQE", "PL", "cited"},
    ("devices", "device_role"): {"control", "target", "record"},
    ("devices", "device_type"): {"cell", "minimodule", "module"},
    ("devices", "architecture"): {"n-i-p", "p-i-n", "substrate", "superstrate"},
    ("devices", "substrate_type"): {"rigid", "flexible"},
    ("devices", "area_definition"): {"designated illumination", "aperture", "total"},
    ("layers", "layer_function"): {"front_contact", "ETL", "absorber", "passivation", "HTL", "interlayer", "recombination", "back_contact"},
    ("measurements", "measurement_type"): {"J-V", "MPP tracking", "EQE", "stability"},
    ("measurements", "scan_direction"): {"reverse", "forward", "average"},
    ("measurements", "stability_protocol"): {"ISOS-D", "ISOS-L", "ISOS-T", "other"},
    ("measurements", "mask_used"): {"true", "false"},
    ("measurements", "certified"): {"true", "false"},
    ("results", "aggregation"): {"champion", "mean", "median"},
    ("results", "result_origin"): {"primary", "cited"},
    ("results", "extraction_status"): {"extracted", "present_not_extractable", "absent"},
}

# Target units and exact, explicitly unit-driven conversion factors.
UNITS = {
    "PCE": ("%", {"%": "1", "fraction": "100"}),
    "FF": ("%", {"%": "1", "fraction": "100"}),
    "HI": ("%", {"%": "1", "fraction": "100"}),
    "retained_PCE": ("%", {"%": "1", "fraction": "100"}),
    "stabilized_PCE": ("%", {"%": "1", "fraction": "100"}),
    "Voc": ("V", {"V": "1", "mV": ".001"}),
    "Jsc": ("mA/cm2", {"mA/cm2": "1", "A/cm2": "1000", "A/m2": ".1"}),
    "Jsc_EQE": ("mA/cm2", {"mA/cm2": "1", "A/cm2": "1000", "A/m2": ".1"}),
    "Rs": ("Ohm*cm2", {"Ohm*cm2": "1", "Ohm*m2": "10000"}),
    "Rsh": ("Ohm*cm2", {"Ohm*cm2": "1", "Ohm*m2": "10000"}),
    "T80": ("h", {"h": "1", "min": "0.01666666666666666666666666667", "s": "0.0002777777777777777777777777778"}),
}
