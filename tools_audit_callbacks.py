"""Dev-only static audit: which inline-keyboard callbacks nothing handles.

Not part of the bot runtime. Run with:  python tools_audit_callbacks.py
"""
import ast, pathlib, collections

root = pathlib.Path(__file__).resolve().parent
files = [p for p in root.rglob('*.py')
         if '.git' not in p.parts and 'venv' not in p.parts and '__pycache__' not in p.parts]

produced = collections.defaultdict(set)
for p in files:
    try:
        tree = ast.parse(p.read_text())
    except SyntaxError as e:
        print("PARSE FAIL", p, e); continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call): continue
        fn = node.func
        name = getattr(fn, 'attr', getattr(fn, 'id', ''))
        if name != 'InlineKeyboardButton': continue
        cb = None
        for kw in node.keywords:
            if kw.arg == 'callback_data': cb = kw.value
        if cb is None: continue
        if isinstance(cb, ast.Constant) and isinstance(cb.value, str):
            produced[cb.value].add(str(p.relative_to(root)))
        elif isinstance(cb, ast.JoinedStr):
            parts = []
            for v in cb.values:
                parts.append(v.value if isinstance(v, ast.Constant) else '*')
            produced[''.join(parts)].add(str(p.relative_to(root)))
        else:
            produced['<dynamic>'].add(str(p.relative_to(root)))

handled_top = collections.defaultdict(set)
handled_pair = collections.defaultdict(set)

def varname(n):
    if isinstance(n, ast.Name): return n.id
    if isinstance(n, ast.Subscript): return varname(n.value) + '[]'
    return None

def rec(stmts, conds, src):
    for st in stmts:
        if isinstance(st, ast.If):
            new = dict(conds)
            t = st.test
            tests = t.values if isinstance(t, ast.BoolOp) else [t]
            for tt in tests:
                if isinstance(tt, ast.Compare) and len(tt.ops) == 1 and isinstance(tt.ops[0], ast.Eq):
                    l, r = tt.left, tt.comparators[0]
                    if isinstance(r, ast.Constant) and isinstance(r.value, str):
                        vn = varname(l)
                        if vn in ('action', 'parts[]'): new[vn] = r.value
                    if isinstance(l, ast.Constant) and isinstance(l.value, str):
                        vn = varname(r)
                        if vn in ('action', 'parts[]'): new[vn] = l.value
                # `action in ("a", "b")` tuple-membership form
                if isinstance(tt, ast.Compare) and len(tt.ops) == 1 and isinstance(tt.ops[0], ast.In):
                    left, right = tt.left, tt.comparators[0]
                    if isinstance(right, (ast.Tuple, ast.List, ast.Set)):
                        vn = varname(left)
                        if vn in ('action', 'parts[]'):
                            for elt in right.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    extra = dict(new)
                                    extra[vn] = elt.value
                                    handled_top[extra['action']].add(src)
                                    if 'parts[]' in extra:
                                        handled_pair[(extra['action'], extra['parts[]'])].add(src)
                                    if vn == 'action' and 'parts[]' in extra:
                                        pass
            if 'action' in new:
                handled_top[new['action']].add(src)
                if 'parts[]' in new:
                    handled_pair[(new['action'], new['parts[]'])].add(src)
            rec(st.body, new, src)
            rec(st.orelse, dict(conds), src)
        else:
            for child in ast.iter_child_nodes(st):
                if isinstance(child, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                    rec([child], conds, src)

for p in files:
    try: tree = ast.parse(p.read_text())
    except SyntaxError: continue
    src = str(p.relative_to(root))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                'handle_callbacks', 'admin_callback', 'handle_promo_callback'):
            rec(node.body, {}, src)
            for n2 in ast.walk(node):
                if isinstance(n2, ast.Call) and isinstance(n2.func, ast.Attribute) and n2.func.attr == 'startswith':
                    if varname(n2.func.value) == 'action' and n2.args:
                        try: v = ast.literal_eval(n2.args[0])
                        except Exception: continue
                        if isinstance(v, str): handled_top[v.rstrip(':')].add(src)

prod_pref = collections.defaultdict(set)
for cb, srcs in produced.items():
    if cb == '<dynamic>': continue
    prod_pref[cb.split(':')[0]].update(srcs)

print("### Produced top-level actions with NO handler branch (DEAD):")
for pref in sorted(prod_pref):
    if pref not in handled_top:
        print(f"  {pref:26s} <- {sorted(prod_pref[pref])[:2]}")

print("\n### Produced (action, sub) not handled:")
for cb, srcs in sorted(produced.items()):
    if cb == '<dynamic>': continue
    parts = cb.split(':')
    if len(parts) < 2 or parts[0] not in handled_top: continue
    if (parts[0], parts[1]) not in handled_pair:
        print(f"  {cb:42s} <- {sorted(srcs)[:2]}")
