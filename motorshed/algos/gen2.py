""" "Gen2" algorithms, which try to minimize routing calls by
inferring routes using the transit times from the much faster
Table API."""

import numpy as np
import osmnx as ox
import pandas as pd
from contexttimer import Timer

from motorshed import osrm


#
# def do_v2_routing(G, center_node, origin_point):
#     """ G must already have the transit times calculated. """


def create_initial_dataframes(G):
    """
    Convert graph G into two geodataframes Gn and Ge (nodes and edges)
    that are easier to do calculations on. Add a few useful columns, and
    make sure that data types are correct.

    ###
    # Gn columns:  (NODES)
    # y               float64    spherical mercator coords?
    # x               float64    spherical mercator coords?
    # osmid             int64    node ID
    # highway          object
    # ref              object
    # lon             float64    longitude
    # lat             float64    latitude
    # calculated         bool    whether it's calculated
    # transit_time    float64    time to get to center node
    # w                 int64

    ##
    # Ge columns:   (EDGES)
    # u                    int64    start node
    # v                    int64    end node
    # key                  int64
    # osmid               object
    # name                object    name (e.g., 'Gerrish Court')
    # highway             object    type of road (e.g., highway? residential?)
    # oneway                bool    is one way?
    # length             float64    length, meters
    # through_traffic      int64    count of cars through this edge
    # geometry            object    complex geometry object
    # lanes               object
    # ref                 object
    # maxspeed            object
    # bridge              object    is a bridge?
    # w                    int64    where traffic is routed to -- Next edge is (v, w)
    # v2                   int64
    # dtype: object
    """
    with Timer(prefix="Create initial dataframes"):

        # Graph -> geodataframes
        Gn, Ge = ox.graph_to_gdfs(G, node_geometry=False, fill_edge_geometry=False)

        ## Fix up Gn  ( NODES dataframe )
        Gn["w"] = 0
        Gn["calculated"] = False
        # Coerce types of geodataframe to what we want
        for f, t in (
            ("transit_time", float),
            ("calculated", bool),
            ("lat", float),
            ("lon", float),
            ("osmid", int),
            ("x", float),
            ("y", float),
            ("highway", str),
        ):
            Gn[f] = Gn[f].astype(t)

        ## Fix up Ge ( EDGES dataframe )
        Ge["w"] = 0  # Next edge is (v, w)
        Ge["v2"] = 0
        Ge["through_traffic"] = 0
        # Coerce types of geodataframe to what we want
        for f, t in (
            ("through_traffic", int),
            ("u", np.long),
            ("v", np.long),
            ("v2", np.long),
        ):
            Ge[f] = Ge[f].astype(t)
        Ge.highway = Ge.highway.map(str)

    return Gn, Ge


def initial_routing(Ge, Gn):
    with Timer(prefix="Initial routing using heuristics"):
        # Grab edge's start & end time from nodes.
        Ge["start_time"] = Gn.loc[Ge.u].transit_time.values
        Ge["end_time"] = Gn.loc[Ge.v].transit_time.values
        # dt is how much transit times changes when this edge is traversed.
        #   If negative, then we made progress.
        Ge["dt"] = Ge["end_time"] - Ge["start_time"]

        # Create multiindex
        ### Q: should we sort 'length'? or keep all copies?  << done >>
        ###   Might this cause problems later w/ missing segments?
        Ge = Ge.sort_values("length", ascending=False).groupby(["u", "v"]).first()

        # Cleanup:
        # Remove edges that point to themselves, which seem to be
        #   mostly cul-de-sacs
        # Remove routes that have duplicates by keeping only the shorter on.
        #   These are mostly U-shaped side routes/detours.

        # Ignore streets that we know will have traffic b/c they: footways; and service roads.
        #  (unless we are later routed through them)
        Ge["ignore"] = False
        Ge.ignore = (
            Ge.ignore
            # | (Ge.dt > 0)  # take us further away;
            | Ge.highway.str.contains("footway")  # shouldn't route here
            | Ge.highway.str.contains("service")  # shouldn't route here
            | Ge.highway.str.contains("path")  # shouldn't route here
            | Ge.highway.str.contains("driveway")  # shouldn't route here
        )

        # Gives us back an unambiguous index (integers, 0..len(Ge)) but keeps u,v as cols
        # that we can query more flexibly than if they are in the index
        Ge = Ge.reset_index()

        # In the easy/unambiguous case where exactly *one* of the edges exiting a
        #   node (e.g., intersection) gets us closer to destination, we can make
        #   a simple mapping -- any traffic through node n1 must go along edge e1
        #   to node n2.
        #
        # Our task is to find the 'next step', `w` for every edge that has traffic.
        #   The mapping above answers that question for any edge that ends at n1; the
        #   next step (w) is n2.

        # This groupby gives the end nodes for all the edges that get us closer,
        #   grouped by start node.
        unambiguous_edges_groupby = Ge.query("dt<0").groupby("u").v
        unambiguous_edges = unambiguous_edges_groupby.first()[
            unambiguous_edges_groupby.count() == 1
        ]
        # Now we have a u->v (n1->n2) mapping for unambiguous edges, like:
        # u
        # 25240726       250117169
        # 25240774       250142020
        #                  ...
        # 5089035979     194721949
        # Name: v, Length: 245, dtype: int64

        # We make a copy of 'v' so that we can query it as a column later after 'v'
        #  is subsumed back into the index
        Ge["v2"] = Ge["v"].astype("int")

        # We create a new column, 'w', which contains the 'next step' of (u,v) if
        #  we know it... and is 0 otherwise.
        Ge["w"] = Ge.v.map(unambiguous_edges).fillna(0).astype(np.long)

        # Special value -1 for 'w' means it's the final traffic sink.
        Ge.loc[Ge.end_time == 0, "w"] = -1

        # Re-index Ge as (u,v) for fast access.
        Ge = Ge.set_index(["u", "v"])

        return Ge, Gn


def followup_heuristic_routing(Ge, Gn):
    """ Route edges that aren't clear by recursively searching for alternative
    routes that eventually get us closer. """
    with Timer(prefix="Follow-up routing using heuristics"):
        print(
            f"Need to fix {len(Ge.query('w==0 and ignore==False'))} ambiguous edges (Currently: {len(Ge.query('ignore==True'))} ignored, {len(Ge.query('w!=0 and ignore==False'))} resolved, {len(Ge)} total)"
        )

        def get_options(s, depth):
            """ Recursive enumeration of all routes we can take away from
            this spot. We'll then pick the best one. """
            u, v = s.name
            if depth <= 0:
                return [[s]]
            options = []
            #     if s.w:
            try:
                # next_options = Ge.query('u==@v and v==@s.w') if s.w else Ge.query('u==@v')
                # here, should we ignore footpaths, etc?
                if s.w:
                    next_options = Ge.loc[([v], [s.w]), :]
                else:
                    next_options = Ge.loc[[v], :]
            except KeyError:
                return [[s]]
            #     else:
            #         next_options = Ge.loc[(v, slice(None))]#.query('u==@v')
            for (up, vp), sp in next_options.iterrows():
                options += [[s] + spp for spp in get_options(sp, depth - 1)]
            return options

        def option_length(option):
            return sum([o.length for o in option])

        # Loop through every un-calculated edge.
        df_to_fix = Ge.query("w==0 and ignore==False").sort_values(
            "end_time", ascending=False
        )
        for i, ((u, v), s) in enumerate(df_to_fix.iterrows()):
            if Ge.loc[(u, v), "w"] != 0:
                continue  # might have been filled in by previous loops
            if i % 100 == 0:
                print(f"{i}/{len(df_to_fix)}")

            # Search out farther and farther until choice becomes clear.
            for n in range(1, 4, 1):
                options = get_options(s, n)
                if not len(options):
                    # Ge.loc[(u, v), "ignore"] = True  # orphan edge
                    break

                df = pd.DataFrame.from_dict(
                    {
                        i: {"dt": o[-1].end_time - s.end_time, "l": option_length(o)}
                        for i, o in enumerate(options)
                    },
                    orient="index",
                )

                df = df.query("dt<0")
                df.loc[:, "efficiency"] = df.dt / df.l
                df = df.sort_values("efficiency")
                if not len(df):
                    continue

                # cool - we found a path that gets us closer. Let's route
                #  accordingly.

                option = options[df.index[0]]
                for i, step in enumerate(option[:-1]):
                    assert (step.w == 0) or (step.w == option[i + 1].name[1])
                    Ge.loc[step.name, "w"] = option[i + 1].name[1]
                #             print('Found new sub-path w/ depth %d: \n%s' % (n, str(df.iloc[0])))
                break
            else:
                print(RuntimeWarning("Couldn" "t find workable option by n==%d" % n))
                ### Should we do an OSRM route lookup here?
                ## Might be efficient, especially if we started with the
                ## furthest-away ambiguous edge...

    return Ge, Gn


def followup_osrm_routing(G, Ge, Gn, center_node, min_iter=50, max_iter=1000):
    """ Use OSRM routing API calls to fix any remaining unsolved edges."""

    all_v_values = set(Ge.index.get_level_values(1))

    with Timer(prefix="Fix missing bits with OSRM"):
        for i in range(max_iter):
            # alternates furthest and closest and semi-random unsolved points
            if i % 2:
                df_unsolved = Ge.query("w==0 and ignore==False").sort_values(
                    "start_time", ascending=i % 3
                )
            else:
                df_unsolved = Ge.query("w==0 and ignore==False").sort_values(
                    "v2", ascending=i % 3
                )
            print("There are %d unsolved edges." % len(df_unsolved))

            if len(df_unsolved) == 0:
                # No missing edges! Choose a random one.
                if i > min_iter:
                    break
                df_unsolved = Ge.sample(n=1)

            uu, vv = df_unsolved.index[0]

            #     print('Solving from node %d' % vv)
            try:
                route, transit_time, r = osrm.osrm(
                    G, vv, center_node, [], mode="driving", private_host=False
                )
            except Exception as e:
                print(e)
                df_unsolved.loc[df_unsolved.index[0], "ignore"] = True
            #     print('Route length: %d' % len(route))
            nodes = set(Ge.index.get_level_values(1)).union(
                Ge.index.get_level_values(0)
            )
            rroute = list(filter(lambda e: e in nodes, route))
            if rroute[0] != vv:
                rroute = [vv] + rroute
            rroute = [uu] + rroute
            print("Filtered route length: %d" % len(rroute))
            for i in range(len(rroute) - 2):
                u, v, w = rroute[i : i + 3]
                # if (u, v) in Ge.index:
                # Specify next edge after this one
                #             if Ge.loc[(u, v), 'w'] != 0 and Ge.loc[(u, v), 'w'] != w:
                #                 print(Ge.loc[(u, v)])
                #                 print(w)
                #                 print('--')
                try:
                    if Ge.loc[(u, v), "w"] != w:
                        print(f"Fix {(u, v)}, w {Ge.loc[(u, v), 'w']} => {w}")
                        Ge.loc[(u, v), "w"] = w
                except KeyError as e:
                    print("%s missing. (w: %s)" % ((u, v), w))
                    j = i
                    while j < len(rroute) - 2:
                        vv, ww = (rroute[j + 1], rroute[j + 2])
                        if (vv, ww) in Ge.index:
                            # v2 indicates that this edge skips ahead to v2
                            Ge.loc[(u, v), "v2"] = int(vv)
                            Ge.loc[(u, v), "w"] = int(ww)
                            print("!")
                            break
                        j += 1
                    else:
                        print("!!!!")

                #         if (v, w) not in Ge.index:
                #             print('pow!')
                #             raise Exception()

                # Also, make all edges that end at 'v' also go on to w unless they already go somewhere else.
                if v in all_v_values:
                    edges_to_v = Ge.loc[(slice(None), v), "w"]
                    n_to_fix = (edges_to_v == 0).sum()
                    if n_to_fix:
                        print(f"({n_to_fix})")
                        edges_to_v.loc[edges_to_v == 0] = w
                        Ge.loc[(slice(None), v), "w"] = edges_to_v

    return Ge


def followup_osrm_routing_parallel(G, Ge, Gn, center_node, min_iter=5, max_iter=100):
    """ Use OSRM routing API calls to fix any remaining unsolved edges."""

    BATCH_SIZE = 25
    N_WORKERS = 5
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_WORKERS) as executor:

        with Timer(prefix="Fix missing bits with OSRM"):
            for i in range(max_iter):
                df_unsolved = Ge.query("w==0 and ignore==False")
                print("There are %d unsolved edges." % len(df_unsolved))

                df_to_solve = df_unsolved.sample(min(BATCH_SIZE, len(df_unsolved)))

                n_extra_needed = BATCH_SIZE - len(df_to_solve)
                if n_extra_needed:
                    df_to_solve = df_to_solve.append(
                        Ge.query("w > 0 and ignore==False").sample(n_extra_needed)
                    )

                node_ids = df_to_solve.index.get_level_values("v").unique()

                nodes = set(Ge.index.get_level_values(1)).union(
                    Ge.index.get_level_values(0)
                )

                future_to_node = {
                    executor.submit(
                        osrm.osrm, G, G.nodes[node_id], center_node
                    ): node_id
                    for node_id in node_ids
                }

                routings = []
                for future in concurrent.futures.as_completed(future_to_node):
                    v = future_to_node[future]
                    try:
                        route, transit_time, r = future.result()
                    except Exception as exc:
                        print(exc)
                        continue

                    for uu, vv in df_to_solve.loc[pd.IndexSlice[:, [v]], :].index:
                        rroute = list(filter(lambda e: e in nodes, route))
                        if rroute[0] != vv:
                            rroute = [vv] + rroute
                        rroute = [uu] + rroute

                        routings += [rroute[i : i + 3] for i in range(len(rroute) - 2)]

                dfroutings = pd.DataFrame(routings, columns=["u", "v", "w"])
                # de-dup, taking most common 'w' if there are multiples
                dfroutings2 = dfroutings.groupby(["u", "v"]).agg(
                    lambda x: pd.Series.mode(x)[0]
                )

                common_index = dfroutings2.index.intersection(Ge.index)
                missing_index = dfroutings2.index.difference(Ge.index)
                for (u, v) in missing_index:

                    uu, vv = u, v

                    for i in range(500):
                        ww = dfroutings2.loc[(uu, vv)].item()

                        if (vv, ww) in Ge.index:
                            Ge.loc[(u, v), "v2"] = int(vv)
                            Ge.loc[(u, v), "w"] = int(ww)
                            print(f"!{i}")
                            break
                        else:
                            uu, vv = vv, ww
                    else:
                        # raise Exception()
                        print(f'Oh crap! {i}')
                n_new_solved = len(common_index.intersection(df_unsolved.index))
                print(f"Solved {n_new_solved} new edges.")
                Ge.loc[common_index, "w"] = dfroutings2.loc[common_index, "w"]

                if (len(df_unsolved) == 0) and (i >= min_iter):
                    break

    return Ge


def propagate_edges(Ge):
    # Reset index to a dummy integer index for faster/easier access
    Gge = Ge.copy().reset_index()

    # Make a mapping from ('u','v') to this new dummy index
    edge_mapping = Gge[["u", "v"]].copy()
    edge_mapping["edge_idx"] = edge_mapping.index
    edge_mapping = edge_mapping.set_index(["u", "v"])

    Gge["through_traffic"] = 0
    Gge["current_traffic"] = 0  # Amount of traffic on that edge
    valid_edges = Gge.query("w != 0")
    # Each non-ignored edge gets 1 car, plus another 1 car per every 50 m of length.
    Gge.loc[valid_edges.index, "current_traffic"] = 1 + valid_edges.length / 50

    old_status = ""
    with Timer(prefix="Propagate Edges"):
        while True:
            edges_to_propagate = (
                Gge.query("(current_traffic > 0)")  # .reset_index()  # & (w != 0)")
                .loc[:, ["u", "v", "v2", "w", "current_traffic"]]
                .copy()
            )

            if not len(edges_to_propagate):
                break

            try:
                assert (edges_to_propagate.w != 0).all()
                status = "Edges to propagate: %d. Traffic: %d. Cars on road: %d." % (
                    len(edges_to_propagate),
                    edges_to_propagate.current_traffic.mean(),
                    edges_to_propagate.current_traffic.sum(),
                )
                print(status)
                if status == old_status:
                    print("Looping. STOP!")
                    raise Exception()
                    break
                old_status = status
            except Exception as e:
                raise
                pass

            # Tally up current traffic
            Gge.loc[
                edges_to_propagate.index, "through_traffic"
            ] += edges_to_propagate.current_traffic

            # Zero current traffic on main copy
            Gge.current_traffic = 0

            # Propagate current traffic to next step
            traffic = (
                edges_to_propagate.query("w>0")
                .groupby(["v2", "w"])
                .current_traffic.sum()
            )
            dummyindexed_traffic = pd.Series(
                traffic.values,
                index=edge_mapping.loc[traffic.index, "edge_idx"].values.astype(int),
            )
            Gge.loc[
                dummyindexed_traffic.index, "current_traffic"
            ] = dummyindexed_traffic

            # for v, w in dfvw.index:
            #     # I think this can be vectorized: Gge.loc[dfvw.index, 'tmp'] = Gge.loc[dfvw.index, 'tmp'] + dfvw.values
            #     #  but Pandas was throwing warnings...
            #     #         Gge.loc[dfvw.index, 'tmp'] = Gge.loc[dfvw.index, 'tmp'] + dfvw.values
            #     if (v, w) not in Gge.index:
            #         #             pass
            #         print(v, w)
            #         raise Exception()
            #     else:
            #         Gge.loc[(v, w), "current_traffic"] = Gge.loc[(v, w), "current_traffic"] + dfvw.loc[(v, w)]
        #             raise Exception()
        #         print(Gge.loc[(v,w)])

        #     if (v,w) in dfuv.index:
        # Gge.loc[edges_to_propagate.set_index(['u', 'v']).index]
        #     raise Exception()
        # Gge = Gge.reset_index()
    return Gge
