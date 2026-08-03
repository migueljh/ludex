SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: action_source; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.action_source AS ENUM (
    'agent',
    'human',
    'opponent'
);


--
-- Name: battle_result; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.battle_result AS ENUM (
    'win',
    'loss',
    'tie'
);


--
-- Name: battle_source; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.battle_source AS ENUM (
    'challenge',
    'ladder',
    'local',
    'import',
    'test'
);


--
-- Name: played_by_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.played_by_kind AS ENUM (
    'bot',
    'human'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: abilities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.abilities (
    id integer NOT NULL,
    gen_id integer NOT NULL,
    showdown_id text NOT NULL,
    name text NOT NULL,
    description text
);


--
-- Name: abilities_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.abilities_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: abilities_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.abilities_id_seq OWNED BY public.abilities.id;


--
-- Name: battle_turns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.battle_turns (
    battle_id integer NOT NULL,
    player_side text NOT NULL,
    turn_number integer NOT NULL,
    protocol_lines text[] NOT NULL,
    agent_reasoning jsonb
);


--
-- Name: battles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.battles (
    id integer NOT NULL,
    battle_tag text NOT NULL,
    tournament_id integer,
    round_id integer,
    format text NOT NULL,
    p1 text NOT NULL,
    p2 text NOT NULL,
    winner text,
    played_by public.played_by_kind NOT NULL,
    source public.battle_source NOT NULL,
    replay_url text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    identity_key text NOT NULL
);


--
-- Name: battles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.battles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: battles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.battles_id_seq OWNED BY public.battles.id;


--
-- Name: generations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.generations (
    id integer NOT NULL,
    gen_number integer NOT NULL,
    label text NOT NULL
);


--
-- Name: generations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.generations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: generations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.generations_id_seq OWNED BY public.generations.id;


--
-- Name: items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.items (
    id integer NOT NULL,
    gen_id integer NOT NULL,
    showdown_id text NOT NULL,
    name text NOT NULL,
    description text,
    properties jsonb NOT NULL
);


--
-- Name: items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.items_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.items_id_seq OWNED BY public.items.id;


--
-- Name: learnsets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.learnsets (
    pokemon_id integer NOT NULL,
    move_id integer NOT NULL,
    learn_methods jsonb NOT NULL
);


--
-- Name: moves; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.moves (
    id integer NOT NULL,
    gen_id integer NOT NULL,
    showdown_id text NOT NULL,
    name text NOT NULL,
    type text NOT NULL,
    category text NOT NULL,
    power integer NOT NULL,
    accuracy integer,
    pp integer NOT NULL,
    priority integer NOT NULL,
    target text NOT NULL,
    flags jsonb NOT NULL,
    description text,
    power_kind text,
    CONSTRAINT moves_power_kind_check CHECK ((power_kind = ANY (ARRAY['status'::text, 'variable'::text, 'fixed_damage'::text, 'special'::text, 'standard'::text])))
);


--
-- Name: COLUMN moves.power_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.moves.power_kind IS 'Derivado por el seed, en este orden: status si category=Status; variable si tiene basePowerCallback; fixed_damage si tiene damage numerico o ''level''; special si basePower=0 y ninguna anterior; standard si basePower>0. NULL solo entre esta migracion y el proximo seed de la generacion.';


--
-- Name: moves_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.moves_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: moves_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.moves_id_seq OWNED BY public.moves.id;


--
-- Name: pokemon; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pokemon (
    id integer NOT NULL,
    gen_id integer NOT NULL,
    showdown_id text NOT NULL,
    dex_num integer NOT NULL,
    name text NOT NULL,
    base_species text NOT NULL,
    forme text,
    is_default boolean NOT NULL,
    types text[] NOT NULL,
    base_stats jsonb NOT NULL,
    abilities jsonb NOT NULL,
    weight_kg numeric,
    evolves_from text,
    tier text,
    base_species_name text NOT NULL
);


--
-- Name: pokemon_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.pokemon_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: pokemon_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.pokemon_id_seq OWNED BY public.pokemon.id;


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying(128) NOT NULL
);


--
-- Name: seed_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seed_runs (
    id integer NOT NULL,
    gen_id integer NOT NULL,
    package_version text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    row_counts jsonb
);


--
-- Name: seed_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seed_runs_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seed_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.seed_runs_id_seq OWNED BY public.seed_runs.id;


--
-- Name: trajectories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trajectories (
    id integer NOT NULL,
    battle_id integer NOT NULL,
    gen_id integer NOT NULL,
    format text NOT NULL,
    player_side text NOT NULL,
    final_result public.battle_result,
    elo_bucket text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: trajectories_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trajectories_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trajectories_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trajectories_id_seq OWNED BY public.trajectories.id;


--
-- Name: trajectory_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trajectory_steps (
    trajectory_id integer NOT NULL,
    turn_number integer NOT NULL,
    state jsonb NOT NULL,
    state_schema_version integer NOT NULL,
    legal_actions jsonb NOT NULL,
    action_taken jsonb,
    action_source public.action_source NOT NULL,
    reward numeric,
    decision_index integer NOT NULL,
    action_path text,
    CONSTRAINT trajectory_steps_action_path_check CHECK ((action_path = ANY (ARRAY['llm'::text, 'llm_retry'::text, 'fallback'::text])))
);


--
-- Name: type_chart; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.type_chart (
    gen_id integer NOT NULL,
    attacking_type text NOT NULL,
    defending_type text NOT NULL,
    multiplier numeric NOT NULL
);


--
-- Name: usage_stats; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_stats (
    id integer NOT NULL,
    gen_id integer NOT NULL,
    format text NOT NULL,
    pokemon_id integer NOT NULL,
    usage_pct numeric,
    common_sets jsonb
);


--
-- Name: usage_stats_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.usage_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: usage_stats_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.usage_stats_id_seq OWNED BY public.usage_stats.id;


--
-- Name: abilities id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abilities ALTER COLUMN id SET DEFAULT nextval('public.abilities_id_seq'::regclass);


--
-- Name: battles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.battles ALTER COLUMN id SET DEFAULT nextval('public.battles_id_seq'::regclass);


--
-- Name: generations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.generations ALTER COLUMN id SET DEFAULT nextval('public.generations_id_seq'::regclass);


--
-- Name: items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items ALTER COLUMN id SET DEFAULT nextval('public.items_id_seq'::regclass);


--
-- Name: moves id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.moves ALTER COLUMN id SET DEFAULT nextval('public.moves_id_seq'::regclass);


--
-- Name: pokemon id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pokemon ALTER COLUMN id SET DEFAULT nextval('public.pokemon_id_seq'::regclass);


--
-- Name: seed_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seed_runs ALTER COLUMN id SET DEFAULT nextval('public.seed_runs_id_seq'::regclass);


--
-- Name: trajectories id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trajectories ALTER COLUMN id SET DEFAULT nextval('public.trajectories_id_seq'::regclass);


--
-- Name: usage_stats id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_stats ALTER COLUMN id SET DEFAULT nextval('public.usage_stats_id_seq'::regclass);


--
-- Name: abilities abilities_gen_id_showdown_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abilities
    ADD CONSTRAINT abilities_gen_id_showdown_id_key UNIQUE (gen_id, showdown_id);


--
-- Name: abilities abilities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abilities
    ADD CONSTRAINT abilities_pkey PRIMARY KEY (id);


--
-- Name: battle_turns battle_turns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.battle_turns
    ADD CONSTRAINT battle_turns_pkey PRIMARY KEY (battle_id, player_side, turn_number);


--
-- Name: battles battles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.battles
    ADD CONSTRAINT battles_pkey PRIMARY KEY (id);


--
-- Name: battles battles_source_identity_key_uniq; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.battles
    ADD CONSTRAINT battles_source_identity_key_uniq UNIQUE (source, identity_key);


--
-- Name: generations generations_gen_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.generations
    ADD CONSTRAINT generations_gen_number_key UNIQUE (gen_number);


--
-- Name: generations generations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.generations
    ADD CONSTRAINT generations_pkey PRIMARY KEY (id);


--
-- Name: items items_gen_id_showdown_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT items_gen_id_showdown_id_key UNIQUE (gen_id, showdown_id);


--
-- Name: items items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT items_pkey PRIMARY KEY (id);


--
-- Name: learnsets learnsets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learnsets
    ADD CONSTRAINT learnsets_pkey PRIMARY KEY (pokemon_id, move_id);


--
-- Name: moves moves_gen_id_showdown_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.moves
    ADD CONSTRAINT moves_gen_id_showdown_id_key UNIQUE (gen_id, showdown_id);


--
-- Name: moves moves_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.moves
    ADD CONSTRAINT moves_pkey PRIMARY KEY (id);


--
-- Name: pokemon pokemon_gen_id_showdown_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pokemon
    ADD CONSTRAINT pokemon_gen_id_showdown_id_key UNIQUE (gen_id, showdown_id);


--
-- Name: pokemon pokemon_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pokemon
    ADD CONSTRAINT pokemon_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: seed_runs seed_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seed_runs
    ADD CONSTRAINT seed_runs_pkey PRIMARY KEY (id);


--
-- Name: trajectories trajectories_battle_id_player_side_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trajectories
    ADD CONSTRAINT trajectories_battle_id_player_side_key UNIQUE (battle_id, player_side);


--
-- Name: trajectories trajectories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trajectories
    ADD CONSTRAINT trajectories_pkey PRIMARY KEY (id);


--
-- Name: trajectory_steps trajectory_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trajectory_steps
    ADD CONSTRAINT trajectory_steps_pkey PRIMARY KEY (trajectory_id, decision_index);


--
-- Name: type_chart type_chart_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.type_chart
    ADD CONSTRAINT type_chart_pkey PRIMARY KEY (gen_id, attacking_type, defending_type);


--
-- Name: usage_stats usage_stats_gen_id_format_pokemon_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_stats
    ADD CONSTRAINT usage_stats_gen_id_format_pokemon_id_key UNIQUE (gen_id, format, pokemon_id);


--
-- Name: usage_stats usage_stats_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_stats
    ADD CONSTRAINT usage_stats_pkey PRIMARY KEY (id);


--
-- Name: battles_created_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX battles_created_at_idx ON public.battles USING btree (created_at DESC);


--
-- Name: battles_source_battle_tag_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX battles_source_battle_tag_idx ON public.battles USING btree (source, battle_tag);


--
-- Name: learnsets_move_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX learnsets_move_id_idx ON public.learnsets USING btree (move_id);


--
-- Name: moves_gen_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX moves_gen_type_idx ON public.moves USING btree (gen_id, type);


--
-- Name: pokemon_gen_base_species_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pokemon_gen_base_species_idx ON public.pokemon USING btree (gen_id, base_species);


--
-- Name: pokemon_gen_dex_num_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pokemon_gen_dex_num_idx ON public.pokemon USING btree (gen_id, dex_num);


--
-- Name: trajectory_steps_version_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX trajectory_steps_version_idx ON public.trajectory_steps USING btree (state_schema_version);


--
-- Name: abilities abilities_gen_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.abilities
    ADD CONSTRAINT abilities_gen_id_fkey FOREIGN KEY (gen_id) REFERENCES public.generations(id) ON DELETE CASCADE;


--
-- Name: battle_turns battle_turns_battle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.battle_turns
    ADD CONSTRAINT battle_turns_battle_id_fkey FOREIGN KEY (battle_id) REFERENCES public.battles(id) ON DELETE CASCADE;


--
-- Name: items items_gen_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT items_gen_id_fkey FOREIGN KEY (gen_id) REFERENCES public.generations(id) ON DELETE CASCADE;


--
-- Name: learnsets learnsets_move_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learnsets
    ADD CONSTRAINT learnsets_move_id_fkey FOREIGN KEY (move_id) REFERENCES public.moves(id) ON DELETE CASCADE;


--
-- Name: learnsets learnsets_pokemon_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learnsets
    ADD CONSTRAINT learnsets_pokemon_id_fkey FOREIGN KEY (pokemon_id) REFERENCES public.pokemon(id) ON DELETE CASCADE;


--
-- Name: moves moves_gen_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.moves
    ADD CONSTRAINT moves_gen_id_fkey FOREIGN KEY (gen_id) REFERENCES public.generations(id) ON DELETE CASCADE;


--
-- Name: pokemon pokemon_gen_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pokemon
    ADD CONSTRAINT pokemon_gen_id_fkey FOREIGN KEY (gen_id) REFERENCES public.generations(id) ON DELETE CASCADE;


--
-- Name: seed_runs seed_runs_gen_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seed_runs
    ADD CONSTRAINT seed_runs_gen_id_fkey FOREIGN KEY (gen_id) REFERENCES public.generations(id) ON DELETE CASCADE;


--
-- Name: trajectories trajectories_battle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trajectories
    ADD CONSTRAINT trajectories_battle_id_fkey FOREIGN KEY (battle_id) REFERENCES public.battles(id) ON DELETE CASCADE;


--
-- Name: trajectories trajectories_gen_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trajectories
    ADD CONSTRAINT trajectories_gen_id_fkey FOREIGN KEY (gen_id) REFERENCES public.generations(id);


--
-- Name: trajectory_steps trajectory_steps_trajectory_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trajectory_steps
    ADD CONSTRAINT trajectory_steps_trajectory_id_fkey FOREIGN KEY (trajectory_id) REFERENCES public.trajectories(id) ON DELETE CASCADE;


--
-- Name: type_chart type_chart_gen_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.type_chart
    ADD CONSTRAINT type_chart_gen_id_fkey FOREIGN KEY (gen_id) REFERENCES public.generations(id) ON DELETE CASCADE;


--
-- Name: usage_stats usage_stats_gen_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_stats
    ADD CONSTRAINT usage_stats_gen_id_fkey FOREIGN KEY (gen_id) REFERENCES public.generations(id) ON DELETE CASCADE;


--
-- Name: usage_stats usage_stats_pokemon_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_stats
    ADD CONSTRAINT usage_stats_pokemon_id_fkey FOREIGN KEY (pokemon_id) REFERENCES public.pokemon(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20260725000001'),
    ('20260725000002'),
    ('20260725000003'),
    ('20260726000001'),
    ('20260727000005'),
    ('20260727000006'),
    ('20260727000007'),
    ('20260727000008'),
    ('20260729000001');
