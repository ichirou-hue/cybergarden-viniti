function recordKey(publication, id) {
  return JSON.stringify([publication, id]);
}

function index(rows, idField) {
  return new Map(rows.map(row => [recordKey(row.publication_id, row[idField]), row]));
}

function group(rows, idField) {
  const groups = new Map();
  for (const row of rows) {
    const key = recordKey(row.publication_id, row[idField]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  return groups;
}

export function solarCatalog(tables) {
  const publications = new Map(tables.publications.map(row => [row.publication_id, row]));
  const devices = index(tables.devices, "device_id");
  const materials = index(tables.materials, "material_id");
  const measurements = index(tables.measurements, "measurement_id");
  const layers = group(tables.layers, "device_id");
  const issues = group(tables.issues.flatMap(issue => {
    const ids = String(issue.record_ids ?? "").split(/[;,|\s]+/).filter(Boolean);
    return ids.map(result_id => ({ ...issue, result_id }));
  }), "result_id");

  return tables.results.map(result => {
    const publication = result.publication_id;
    const deviceKey = recordKey(publication, result.device_id);
    const device = devices.get(deviceKey);
    const materialId = result.material_id || device?.absorber_material_id;
    return {
      result,
      publication: publications.get(publication),
      device,
      material: materials.get(recordKey(publication, materialId)),
      measurement: measurements.get(recordKey(publication, result.measurement_id)),
      layers: [...(layers.get(deviceKey) ?? [])].sort((left, right) =>
        (Number(left.layer_order) || Infinity) - (Number(right.layer_order) || Infinity)),
      issues: issues.get(recordKey(publication, result.result_id)) ?? [],
      source: {
        page: result.source_page,
        section: result.source_section,
        table: result.source_table,
        figure: result.source_figure,
        quote: result.source_fragment,
      },
    };
  });
}
