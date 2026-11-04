"""Associate recognized native PDF notes with the section where they appear."""
import re
from .daily_settlement_pdf import source_qualifications


def source_qualifications_by_scope(text):
    """Return known notes by observed table or narrative, retaining document notes.

    Missing or ambiguous boundaries are reported; unknown notes remain in the
    original PDF and layout rather than being assigned to a guessed section.
    """
    headings=('发电侧现货日结算总体情况及分类构成情况',
              '用户侧现货日结算总体情况及分类构成情况',
              '用户侧现货日结算相关基础数据情况')
    matches=[re.search(r'(?m)^[ \t\f]*表[ \t]*'+str(i)+r'[ \t]*'+re.escape(heading)+r'[ \t]*$',text)
             for i,heading in enumerate(headings,1)]
    if any(x is None for x in matches[:2]) or matches[0].start()>=matches[1].start():
        return {'status':'unresolved_table_boundaries','scopes':{}}
    if matches[2] is not None and matches[2].start()<=matches[1].start():
        return {'status':'unresolved_table_boundaries','scopes':{}}
    first=matches[0];second=matches[1];third=matches[2]
    between=text[first.end():second.start()]
    split=list(re.finditer(r'二、\s*用户侧结算情况',between))
    if len(split)!=1:return {'status':'unresolved_narrative_boundary','scopes':{}}
    user_start=first.end()+split[0].start()
    sections={'table:1':text[first.end():user_start],
              'table:2':text[second.end():third.start() if third else len(text)],
              'narrative:发电侧':text[:first.start()],
              'narrative:用户侧':text[user_start:second.start()]}
    if third is not None:sections['table:3']=text[third.end():]
    return {'status':'observed_section_boundaries',
            'scopes':{key:source_qualifications(value) for key,value in sections.items()}}


def note_scope_for_row(origin,row):
    if origin=='正文':
        side=row.get('side')
        return 'narrative:'+side if side in ('发电侧','用户侧') else None
    table=row.get('table')
    return 'table:'+str(table) if table in (1,2,3) else None
