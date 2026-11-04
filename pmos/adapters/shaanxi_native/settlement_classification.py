"""Expand verified native settlement category rows into quantities and prices."""
FIELDS=['date','table','side','category','metric','value','unit','sourceValueState','sourceMissingMarker','sourceSha256','scopedQualifications','scopedQualificationRegion','scopedQualificationStatus']
def normalize(categories,scope_result,source_sha):
 rows=[];scopes=scope_result.get('scopes',{})
 for row in categories:
  table=row.get('table');scope='table:'+str(table);known=scope in scopes;base={'date':row.get('date'),'table':table,'side':row.get('side'),'category':row.get('category'),'sourceMissingMarker':row.get('sourceMissingMarker'),'sourceSha256':source_sha,'scopedQualifications':'；'.join(scopes.get(scope,[])),'scopedQualificationRegion':scope,'scopedQualificationStatus':scope_result.get('status') if known else 'unresolved_scope'}
  quantity=row.get('quantity');price=row.get('price');marker=row.get('sourceMissingMarker')
  if quantity is not None and price is not None and not marker:
   rows.extend([{**base,'metric':'结算电量','value':quantity,'unit':row.get('quantityUnit'),'sourceValueState':'observed'},{**base,'metric':'结算均价','value':price,'unit':row.get('priceUnit'),'sourceValueState':'observed'}])
  elif quantity is None and price is None and marker:
   rows.append({**base,'metric':'原表未给数值','value':'','unit':'','sourceValueState':'source_missing_marker'})
  else:raise ValueError('unexpected_category_value_shape')
 return rows
