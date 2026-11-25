-- Gearhead Labs staging validation: two-user access matrix (S2), formula-version compatibility (C2) and immutability (C3).
-- ONE statement, ONE transaction. It always ends by raising the deliberate error GH_TEST_SENTINEL, so nothing it does can
-- commit; the results travel out inside that error message as JSON. Any other ending is a failure (db_check.py).
-- Test users use fixed ids and @example.test addresses; if any already exists the block stops before writing anything.
DO $do$
DECLARE
  ua uuid := 'a0000000-0000-4000-8000-00000000000a';
  ub uuid := 'b0000000-0000-4000-8000-00000000000b';
  uc uuid := 'c0000000-0000-4000-8000-00000000000c';
  res jsonb := '{}'::jsonb;
  ids jsonb := '{}'::jsonb;
  callers text[] := ARRAY['anon','A','B','C'];
  cust text[] := ARRAY['accounts','profiles','garages','machines','machine_details','components','component_connections','test_setups','calculation_records','value_records','saved_calculations','engineering_analyses','entitlement_grants','subscriptions','billing_customers'];
  refs text[] := ARRAY['calculators','canonical_fields','formula_versions','plans','plan_offers','plan_prices','engineering_analyzers','stripe_events'];
  ownercol jsonb := '{"accounts":"id","profiles":"account_id","entitlement_grants":"account_id","subscriptions":"account_id","billing_customers":"account_id"}';
  u uuid; o uuid; who text; t text; oc text; n bigint; nown bigint; noth bigint; st text; uidseen text; r jsonb;
  g uuid; m uuid; c1 uuid; c2 uuid; ts uuid; cr uuid; vr uuid; sc uuid; ea uuid; eg uuid;
BEGIN
  IF EXISTS (SELECT 1 FROM auth.users WHERE id IN (ua, ub, uc) OR email LIKE '%@example.test') THEN
    RAISE EXCEPTION 'GH_TEST_PRECONDITION validation test users already exist: refusing to run';
  END IF;
  -- ---------- seed, as the database owner ----------
  INSERT INTO auth.users (id, email, aud, role) VALUES
    (ua,'gh-val-a@example.test','authenticated','authenticated'),
    (ub,'gh-val-b@example.test','authenticated','authenticated'),
    (uc,'gh-val-c@example.test','authenticated','authenticated');
  FOREACH u IN ARRAY ARRAY[ua,ub] LOOP
    INSERT INTO public.garages (owner_id,name) VALUES (u,'val garage') RETURNING id INTO g;
    INSERT INTO public.machines (owner_id,garage_id,name,machine_type) VALUES (u,g,'val car','automotive') RETURNING id INTO m;
    INSERT INTO public.machine_details (machine_id,owner_id,make) VALUES (m,u,'ValMake');
    INSERT INTO public.components (owner_id,machine_id,kind) VALUES (u,m,'engine') RETURNING id INTO c1;
    INSERT INTO public.components (owner_id,machine_id,kind) VALUES (u,m,'transmission') RETURNING id INTO c2;
    INSERT INTO public.component_connections (owner_id,machine_id,from_component_id,to_component_id,relation) VALUES (u,m,c1,c2,'drives');
    INSERT INTO public.test_setups (owner_id,machine_id,name) VALUES (u,m,'val setup') RETURNING id INTO ts;
    INSERT INTO public.calculation_records (owner_id,machine_id,calculator_id,canonical_id,engine_version,formula_registry,formula_version,result_state,inputs,outputs,request_id)
      VALUES (u,m,'fuel_pump_sizing','fuel_pump_sizing','1.1.0','GH_LEGACY_FORMULAS','fv1-f5c74781','valid','{}','[]',gen_random_uuid()) RETURNING id INTO cr;
    INSERT INTO public.value_records (owner_id,machine_id,canonical_field,numeric_value,unit,provenance,context)
      VALUES (u,m,'block_deck_height',9.0,'in','user_entered','specification') RETURNING id INTO vr;
    INSERT INTO public.saved_calculations (owner_id,calculation_id,title) VALUES (u,cr,'val saved') RETURNING id INTO sc;
    INSERT INTO public.engineering_analyses (owner_id,analyzer_id,analyzer_version,title,inputs,inputs_unit_system)
      SELECT u,analyzer_id,analyzer_version,'val analysis','{}','imperial' FROM public.engineering_analyzers ORDER BY analyzer_id LIMIT 1 RETURNING id INTO ea;
    INSERT INTO public.entitlement_grants (account_id,plan_key,source,note) VALUES (u,'premium','manual','validation test') RETURNING id INTO eg;
    INSERT INTO public.billing_customers (account_id,stripe_customer_id) VALUES (u,'cus_val'||left(u::text,1));
    INSERT INTO public.subscriptions (stripe_subscription_id,account_id,stripe_customer_id,status,stripe_state_at) VALUES ('sub_val'||left(u::text,1),u,'cus_val'||left(u::text,1),'active',now());
    ids := ids || jsonb_build_object(CASE WHEN u=ua THEN 'A' ELSE 'B' END,
      jsonb_build_object('garage',g,'machine',m,'component',c1,'test_setup',ts,'calc',cr,'value',vr,'saved',sc,'analysis',ea,'grant',eg));
  END LOOP;

  -- ---------- reads, as each caller ----------
  FOREACH who IN ARRAY callers LOOP
    u := CASE who WHEN 'A' THEN ua WHEN 'B' THEN ub WHEN 'C' THEN uc END;
    o := CASE who WHEN 'A' THEN ub WHEN 'B' THEN ua ELSE NULL END;
    PERFORM set_config('request.jwt.claims', CASE WHEN u IS NULL THEN '{"role":"anon"}' ELSE json_build_object('sub',u,'role','authenticated')::text END, true);
    EXECUTE CASE WHEN u IS NULL THEN 'SET LOCAL ROLE anon' ELSE 'SET LOCAL ROLE authenticated' END;
    uidseen := coalesce(auth.uid()::text,'null');
    r := jsonb_build_object('uid', uidseen, 'uid_ok', (uidseen = coalesce(u::text,'null')));
    FOREACH t IN ARRAY cust || refs LOOP
      oc := coalesce(ownercol->>t, 'owner_id');
      BEGIN
        IF t = ANY(refs) THEN
          EXECUTE format('select count(*) from public.%I', t) INTO n; nown := NULL; noth := NULL;
        ELSE
          EXECUTE format('select count(*), count(*) filter (where %I::text = $1), count(*) filter (where %I::text is distinct from $1) from public.%I', oc, oc, t)
            INTO n, nown, noth USING coalesce(u::text,'-');
        END IF;
        r := r || jsonb_build_object('read:'||t, jsonb_build_object('total',n,'own',nown,'other',noth));
      EXCEPTION WHEN OTHERS THEN
        r := r || jsonb_build_object('read:'||t, 'ERR '||SQLSTATE);
      END;
    END LOOP;
    RESET ROLE;
    res := res || jsonb_build_object(who, r);
  END LOOP;

  -- ---------- writes, as each caller (each attempt inside its own savepoint) ----------
  FOREACH who IN ARRAY callers LOOP
    u := CASE who WHEN 'A' THEN ua WHEN 'B' THEN ub WHEN 'C' THEN uc END;
    o := CASE who WHEN 'A' THEN ub WHEN 'B' THEN ua ELSE ub END;   -- the "other" owner whose rows we attack
    r := '{}'::jsonb;
    DECLARE
      stmts text[][] := ARRAY[
        ['ins_garage_own',      format($q$insert into public.garages (name) values ('x')$q$)],
        ['ins_machine_own',     format($q$insert into public.machines (garage_id,name,machine_type) values (%L,'x','automotive')$q$, ids->(CASE WHEN u=ub THEN 'B' ELSE 'A' END)->>'garage')],
        ['ins_garage_as_other', format($q$insert into public.garages (owner_id,name) values (%L,'x')$q$, o)],
        ['upd_other_garage',    format($q$update public.garages set name='hijack' where id=%L$q$, ids->(CASE WHEN o=ua THEN 'A' ELSE 'B' END)->>'garage')],
        ['give_own_garage_away',format($q$update public.garages set owner_id=%L where owner_id=auth.uid()$q$, o)],
        ['del_own_garage',      $q$delete from public.garages where owner_id=auth.uid()$q$],
        ['del_other_garage',    format($q$delete from public.garages where id=%L$q$, ids->(CASE WHEN o=ua THEN 'A' ELSE 'B' END)->>'garage')],
        ['upd_other_machine',   format($q$update public.machines set name='hijack' where id=%L$q$, ids->(CASE WHEN o=ua THEN 'A' ELSE 'B' END)->>'machine')],
        ['upd_other_setup',     format($q$update public.test_setups set name='hijack' where id=%L$q$, ids->(CASE WHEN o=ua THEN 'A' ELSE 'B' END)->>'test_setup')],
        ['upd_other_profile',   format($q$update public.profiles set display_name='hijack' where account_id=%L$q$, o)],
        ['softdel_other_garage_rpc', format($q$select public.df_soft_delete_garage(%L)$q$, ids->(CASE WHEN o=ua THEN 'A' ELSE 'B' END)->>'garage')],
        ['ins_calc_record',     $q$insert into public.calculation_records (calculator_id,canonical_id,engine_version,formula_registry,formula_version,result_state,inputs,outputs,request_id) values ('wet_bulb','wet_bulb','1.1.0','GH_LEGACY_FORMULAS','fv1-a2a279c6','valid','{}','[]',gen_random_uuid())$q$],
        ['ins_formula_version', $q$insert into public.formula_versions values ('wet_bulb','fv1-00000000','1.1.0','GH_LEGACY_FORMULAS','X')$q$],
        ['upd_formula_version', $q$update public.formula_versions set first_release='X' where calculator_id='wet_bulb'$q$],
        ['ins_entitlement',     $q$insert into public.entitlement_grants (account_id,plan_key,source) values (auth.uid(),'premium','manual')$q$],
        ['ins_saved_own_calc',  format($q$insert into public.saved_calculations (calculation_id,title) values (%L,'x')$q$, ids->(CASE WHEN u=ua THEN 'A' WHEN u=ub THEN 'B' ELSE 'A' END)->>'calc')],
        ['ins_saved_other_calc',format($q$insert into public.saved_calculations (calculation_id,title) values (%L,'x')$q$, ids->(CASE WHEN o=ua THEN 'A' ELSE 'B' END)->>'calc')],
        ['ins_value_record',    format($q$insert into public.value_records (machine_id,canonical_field,numeric_value,unit,provenance,context) values (%L,'block_deck_height',1,'in','user_entered','specification')$q$, ids->(CASE WHEN u=ub THEN 'B' ELSE 'A' END)->>'machine')],
        ['rpc_handle_new_user', $q$select public.df_handle_new_auth_user()$q$]
      ];
      i int;
    BEGIN
      FOR i IN 1 .. array_length(stmts,1) LOOP
        BEGIN
          PERFORM set_config('request.jwt.claims', CASE WHEN who='anon' THEN '{"role":"anon"}' ELSE json_build_object('sub',u,'role','authenticated')::text END, true);
          EXECUTE CASE WHEN who='anon' THEN 'SET LOCAL ROLE anon' ELSE 'SET LOCAL ROLE authenticated' END;
          EXECUTE stmts[i][2];
          GET DIAGNOSTICS n = ROW_COUNT;
          st := 'OK rows=' || n;
          RESET ROLE;
          RAISE EXCEPTION USING ERRCODE = 'GHSP1', MESSAGE = st;   -- undo this attempt, keep its outcome
        EXCEPTION WHEN OTHERS THEN
          IF SQLSTATE = 'GHSP1' THEN st := SQLERRM; ELSE st := 'REFUSED ' || SQLSTATE || ' ' || left(SQLERRM, 90); END IF;
          RESET ROLE;
        END;
        r := r || jsonb_build_object(stmts[i][1], st);
      END LOOP;
    END;
    res := jsonb_set(res, ARRAY[who], (res->who) || jsonb_build_object('writes', r));
  END LOOP;

  -- ---------- C2 / C3, as the database owner ----------
  DECLARE fp text[][] := ARRAY[
      ['diesel_power_from_fuel','fv1-b16d2d94'],['diesel_smoke_limit','fv1-64771dd0'],['piston_position','fv1-09188b84'],
      ['cranking_compression','fv1-59c91f10'],['wet_bulb','fv1-782fda42'],['rc_fuel_stint','fv1-6d100b74']];
    i int; cid text; canon text; oldfp text;
  BEGIN
    r := '{}'::jsonb;
    FOR i IN 1 .. 6 LOOP
      cid := fp[i][1];
      SELECT canonical_id INTO canon FROM public.calculators WHERE calculator_id = cid;
      SELECT max(formula_version) INTO oldfp FROM public.formula_versions WHERE calculator_id = canon;
      FOREACH t IN ARRAY ARRAY['new_own','old_own','new_other','old_other'] LOOP
        BEGIN
          INSERT INTO public.calculation_records (owner_id,calculator_id,canonical_id,engine_version,formula_registry,formula_version,result_state,inputs,outputs,request_id)
          VALUES (ua, CASE WHEN t LIKE '%_other' THEN 'fuel_pump_sizing' ELSE cid END, CASE WHEN t LIKE '%_other' THEN 'fuel_pump_sizing' ELSE canon END,
                  '1.1.0','GH_LEGACY_FORMULAS', CASE WHEN t LIKE 'old_%' THEN oldfp ELSE fp[i][2] END,'valid','{}','[]',gen_random_uuid());
          RAISE EXCEPTION USING ERRCODE = 'GHSP1', MESSAGE = 'accepted';
        EXCEPTION WHEN OTHERS THEN
          st := CASE WHEN SQLSTATE='GHSP1' THEN 'accepted' ELSE 'refused '||SQLSTATE END;
        END;
        r := r || jsonb_build_object(cid||':'||t||CASE WHEN t LIKE 'old_%' THEN '('||coalesce(oldfp,'none')||')' ELSE '' END, st);
      END LOOP;
      r := r || jsonb_build_object(cid||':new_row_present', EXISTS (SELECT 1 FROM public.formula_versions WHERE calculator_id=canon AND formula_version=fp[i][2]));
    END LOOP;
    res := res || jsonb_build_object('C2', r);
    r := '{}'::jsonb;
    BEGIN UPDATE public.formula_versions SET first_release = 'X' WHERE calculator_id = 'wet_bulb'; st := 'UPDATE ALLOWED';
    EXCEPTION WHEN OTHERS THEN st := 'refused ' || SQLSTATE || ' ' || left(SQLERRM, 80); END;
    r := r || jsonb_build_object('owner_update', st);
    BEGIN DELETE FROM public.formula_versions WHERE calculator_id = 'wet_bulb'; st := 'DELETE ALLOWED';
    EXCEPTION WHEN OTHERS THEN st := 'refused ' || SQLSTATE || ' ' || left(SQLERRM, 80); END;
    r := r || jsonb_build_object('owner_delete', st);
    BEGIN TRUNCATE public.formula_versions CASCADE; st := 'TRUNCATE ALLOWED';
    EXCEPTION WHEN OTHERS THEN st := 'refused ' || SQLSTATE || ' ' || left(SQLERRM, 80); END;
    r := r || jsonb_build_object('owner_truncate', st);
    res := res || jsonb_build_object('C3', r);
  END;

  res := res || jsonb_build_object('fv_count_inside', (SELECT count(*) FROM public.formula_versions));
  RAISE EXCEPTION 'GH_TEST_SENTINEL %', res::text;
END $do$;
