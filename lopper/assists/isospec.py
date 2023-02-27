#/*
# * Copyright (c) 2023 Advanced Micro Devices, Inc. All Rights Reserved.
# *
# * Author:
# *       Bruce Ashfield <bruce.ashfield@amd.com>
# *
# * SPDX-License-Identifier: BSD-3-Clause
# */

import struct
import sys
import types
import unittest
import os
import getopt
import re
import subprocess
import shutil
from pathlib import Path
from pathlib import PurePath
from io import StringIO
import contextlib
import importlib
from lopper import Lopper
from lopper import LopperFmt
from lopper.yaml import LopperJSON
from lopper.tree import LopperAction
from lopper.tree import LopperTree
from lopper.tree import LopperNode
from lopper.tree import LopperProp
import lopper
import lopper_lib
from itertools import chain
import json
import humanfriendly

import logging

logging.basicConfig( format='[%(levelname)s]: %(message)s' )

def is_compat( node, compat_string_to_test ):
    if re.search( "isospec,isospec-v1", compat_string_to_test):
        return isospec_domain
    if re.search( "module,isospec", compat_string_to_test):
        return isospec_domain
    return ""

def _warning( message ):
    logging.warning( message )

def _info( message, output_if_true = True ):
    if output_if_true:
        logging.info( message )

def _error( message, also_exit = True ):
    logging.error( message )
    if also_exit:
        sys.exit(1)

def _debug( message, object_to_print = None ):
    logging.debug( message )
    if object_to_print:
        if logging.getLogger().isEnabledFor( logging.DEBUG ):
            object_to_print.print()

# tests for a bit that is set, going fro 31 -> 0 from MSB to LSB
def check_bit_set(n, k):
    if n & (1 << (k)):
        return True

    return False

def set_bit(value, bit):
    return value | (1<<bit)

def clear_bit(value, bit):
    return value & ~(1<<bit)


def destinations( tree ):
    """returns all nodes with a destinations property in a tree
    """
    nodes_with_dests = []

    # find all the nodes with destinations in a tree. We are walking
    # all the nodes, and checking for a destinations property
    for n in tree:
        try:
            dests = n["destinations"]
            nodes_with_dests.append( n )
        except:
            pass

    return nodes_with_dests


iso_cpus_to_device_tree_map = {
                                "APU*": "arm,cortex-a72",
                                "RPU*": "arm,cortex-r5"
                              }

def isospec_process_cpus( cpus_info, sdt, json_tree ):
    """ returns a list of dictionaries that represent the structure
        of any found cpus. These can be converted to json for future
        encoding into device tree properties
    """
    _info( f"isospec_process_cpus: {cpus_info} [{type(cpus_info)}] [{cpus_info.pclass}]" )

    cpus_list = []
    cpus_json_list = []
    for c in range(len(cpus_info)):
        # this gets us the json chunked and loaded cpu dict
        cpu = cpus_info[c]
        _info( f"    processing cpu: {cpu}" )
        cpu_name = cpu["name"]

        device_tree_compat = ""
        for n,dn in iso_cpus_to_device_tree_map.items():
            if re.search( n, cpu_name ):
                device_tree_compat = dn

        # did we have a mapped compatible string in the device tree ?
        if device_tree_compat:
            # is there a number in the isospec name ? If so, that is our
            # mask, if not, we set the cpu mask to 0x3 (them all)
            m = re.match( r'.*?(\d+)', cpu_name )
            if m:
                cpu_number = m.group(1)
            else:
                cpu_number = -1

            # look in the device tree for a node that matches the
            # mapped compatible string
            compatible_nodes = sdt.tree.cnodes( device_tree_compat )
            if compatible_nodes:
                # we need to find the cluster name / label, that's the parent
                # of the matching nodes, any node will do, so we take the first
                cpu_cluster = compatible_nodes[0].parent
                if not cpu_cluster:
                    _warning( f"no cluster found for cpus, returning" )
                    return None

                # take the label if set, otherwise take the node name
                cluster_name = cpu_cluster.label if cpu_cluster.label else cpu_cluster.name

                # we have the name, now we need the cluster mask. If
                # there's a cpu number. Confirm that the node exists,
                # and set the bit. If there's no number, our mask is
                # 0xf
                cluster_mask = 0
                if cpu_number != -1:
                    for c in compatible_nodes:
                        if re.search( "cpu@" + cpu_number, c.name ):
                            cluster_mask = set_bit( cluster_mask, int(cpu_number) )
                else:
                    cluster_mask = 0xf

                # cpu mode checks.
                #    secure
                #    el
                secure = False
                mode_mask = 0
                try:
                    secure_val = cpu["secure"]
                    secure = secure_val
                except:
                    pass

                try:
                    mode = cpu["mode"]
                    if mode == "el":
                        mode_mask = set_bit( mode_mask, 0 )
                        mode_mask = set_bit( mode_mask, 1 )
                except:
                    pass

                # print( f"secure: {secure} mode: {mode_mask}" )
                cpus_list.append( { "cluster" : cluster_name,
                                    "cpumask" : hex(cluster_mask),
                                    "mode" : { "secure": secure,
                                               "el": hex(mode_mask)
                                               }
                                    }
                                  )
                cpus_json_list.append( f"{{\"cluster\": \"{cluster_name}\", \"cpumask\": \"{hex(cluster_mask)}\", \"mode\": {{\"secure\": {secure}, \"el\": \"{hex(mode_mask)}\"}}}}" )

        else:
            _warning( f"cpus entry {cpus_info[c]} has no device tree mapping" )

    cpus_json_string = "["
    for index,element in enumerate(cpus_json_list):
        cpus_json_string = cpus_json_string + element
        if index != len(cpus_json_list) - 1:
            cpus_json_string = cpus_json_string + ","
        pass
    cpus_json_string = cpus_json_string + "]"

    #_info( "cpus_json_string: %s" % cpus_json_string )
    _info( "cpus_list: %s" % cpus_list )

    return cpus_list

def isospec_device_flags( device_name, defs, json_tree ):

    domain_flag_dict = {}

    # try 1: is it a property ?
    flags = defs.propval( "flags" )

    # try 2: is it a subnode ?
    if not flags[0]:
        for n in defs.children():
            if n.name == "flags":
                for p in n:
                    flags.append( p )

    # map the flags to something domains.yaml can output
    # create a flags dictionary, so we can next it into the access
    # structure below, which will then be transformed into yaml later.
    for flag in flags:
        try:
            if flag.value != '':
                # if a flag is present, it means it was set to "true", it
                # won't even be here in the false case.
                domain_flag_dict[flag.name] = True
        except:
            pass

    _info( "isospec_device_flags: %s %s" % (device_name,domain_flag_dict) )

    return domain_flag_dict

# if something appears in this map, it is a memory entry, and
# we need to process it as such.
iso_memory_device_map = {
                          "DDR0" : ["memory", "memory@.*"],
                          "OCM.*" : ["sram", None]
                        }

def isospec_memory_type( name ):
    mem_found = None
    for n,v in iso_memory_device_map.items():
        if re.search( n, name ):
            mem_found = v

    if mem_found:
        return mem_found[0]

    return ""

def isospec_memory_dest( name ):
    mem_found = None
    for n,v in iso_memory_device_map.items():
        if re.search( n, name ):
            mem_found = v

    if mem_found:
        return mem_found[1]

    return ""

def isospec_process_memory( name, dest, sdt, json_tree ):
    _info( f"isospec_process_memory: {dest}" )
    memory_dest = isospec_memory_dest( name )
    memory_type = isospec_memory_type( name )
    memory_node = None
    memory_list = []
    if memory_type == "memory":
        _info( f"  memory {memory_dest}" )
        # we have a node to lookup in the device tree
        try:
            possible_mem_nodes = sdt.tree.nodes(memory_dest)
        except Exception as e:
            possible_mem_nodes = []
            _info( f"Exception looking for memory: {e}" )

        for n in possible_mem_nodes:
            _info( f"  possible_mem_nodes: {n.abs_path} type: {n['device_type']}" )
            try:
                if "memory" in n["device_type"].value:
                    reg = n["reg"]
                    _info( "  reg %s" % reg.value )

                    # we could do this more generically and look it up in the
                    # parent, but 2 is the default, so doing this for initial
                    # effort
                    address_cells = 2
                    size_cells = 2

                    start = reg.value[0:address_cells]
                    start = lopper.base.lopper_base.encode_byte_array( start )
                    start = int.from_bytes(start,"big")
                    size =  reg.value[address_cells:]
                    size = lopper.base.lopper_base.encode_byte_array( size )
                    size = int.from_bytes(size,"big")

                    _info( f"  start: {hex(start)} size: {hex(size)}" )

                    memory_list.append( {
                                           "start": start,
                                           "size": size
                                         }
                                       )

            except Exception as e:
                _debug( f"Exception {e}" )

    elif memory_type == "sram":
        # no memory dest
        _info( f"sram memory type" )
        address = dest['addr']
        tnode = sdt.tree.addr_node( address )
        if tnode:
            # pull the start and size out of the device tree node
            # don't have a device tree to test this yet
            _warning( f"target node {tnode.abs_path} found, but no processing is available" )
        else:
            size = dest['size']
            # size = humanfriendly.parse_size( size, True )
            start = address
            _info( f"sram start: {start} size: {size}" )
            memory_list.append( {
                                  "start": start,
                                  "size": size
                                }
                              )

    return memory_list

#### TODO: make this take a "type" and only return that type, versus the
####       current multiple list return
def isospec_process_access( access_node, sdt, json_tree ):
    """processes the access values in an isospec subsystem
    """
    access_list = []
    memory_list = []
    sram_list = []
    for access in access_node.children():
        _info( f"process_access: {access.name} [{access.abs_path}]" )
        try:
            same_as_default = access["same_as_default"]

            _info( f"{access.abs_path} has default settings, looking up" )

            # same_as_default was set, we need to locate it
            defs = isospec_device_defaults( access.name, json_tree )
            if not defs:
                _error( "cannot find default settings" )

            # defs.print()

            _info( f"found device defaults: {defs}", defs )
            _debug( f"default has destinations: {defs.propval( 'destinations' )}" )

            flag_mapping = isospec_device_flags( access.name, defs, json_tree )

            # find the destinations in the isospec json tree
            dests = isospec_device_destination( defs.propval("destinations"), json_tree )

            # we now need to locate the destination device in the device tree, all
            # we have is the address to use for the lookup
            for d in dests:
                try:
                    address = d['addr']
                    name = d['name']
                    tnode = sdt.tree.addr_node( address )
                    if tnode:
                        _info( f"    found node at address {address}: {tnode}", tnode )
                        access_list.append( {
                                              "dev": tnode.name,
                                              "flags": flag_mapping
                                            }
                                          )
                    else:
                        raise Exception( f"no node found for {name} => {d}" )
                except Exception as e:
                    mem_found = None
                    for n,v in iso_memory_device_map.items():
                        if re.search( n, d['name'] ):
                            _info( f"    device is memory: {n} matches {d['name']}" )
                            mem_found = v

                    # no warning if we failed on memory in the try clause
                    if mem_found:
                        ml = isospec_process_memory( d['name'], d, sdt, json_tree )
                        if "memory" == isospec_memory_type(d['name']):
                            memory_list.extend( ml )
                        if "sram" == isospec_memory_type(d['name']):
                            sram_list.extend( ml )

                        # no warning for memory
                        continue

                    # it was something other than a dict returned as a dest
                    _warning( f"isospec: process_access: {e}" )

        except Exception as e:
            pass
            # print( "Exception %s" % e )

    return access_list, memory_list, sram_list

def isospec_device_defaults( device_name, isospec_json_tree ):
    """
    returns the default settings for the named device
    """

    default_settings = isospec_json_tree["/default_settings"]
    if not default_settings:
        return None

    default_subsystems = isospec_json_tree["/default_settings/subsystems"]
    if not default_subsystems:
        return None

    default_subsystem = None
    for s in default_subsystems.children():
        if s.name == "default":
            default_subsystem = s

    if not default_subsystem:
        return None

    # we now (finally) have the default subsystem. The subnodes and
    # properties of this node contain our destinations with default
    # values for the various settings

    # if we end up with large domains, we may want to run this once
    # and construct a dictionary to consult later.

    try:
        default_access = [child for child in default_subsystem.children() if child.name == "access"][0]
        device_default = [child for child in default_access.children() if child.name == device_name][0]
    except Exception as e:
        # no settings, return none
        return None

    return device_default

def isospec_device_destination( destination_list, isospec_json_tree ):
    """Look for the isospec "destinations" that match the passed
       list of destinations.

       returns a list of the isospec destinatino that matches
    """

    destination_result = []

    # locate all nodes in the tree that have a destinations property
    dnodes = destinations( isospec_json_tree )

    for destination in destination_list:
        for n in dnodes:
            try:
                dests = n["destinations"]
            except Exception as e:
                pass

            if dests.pclass == "json":
                _debug( f"node {n.abs_path} has json destinations property: {dests.name}" )
                # _info( f"raw dests: {dests.value} ({type(dests.value)})" )
                try:
                    for i in range(len(dests)):
                        x = dests[i]
                        if x["name"] == destination:
                            destination_result.append( x )
                except Exception as e:
                    # it wsn't a dict, ignore
                    pass
            else:
                pass
                # for i in dests.value:
                #     if i == destination:
                #         destination_result.append( i )

    _info( f"destinations found: {destination_result}" )

    return destination_result

def domains_tree_start():
    """ Start a device tree to represent a system device tree domain
    """
    domains_tree = LopperTree()
    domain_node = LopperNode( abspath="/domains", name="domains" )

    return domains_tree

def domains_tree_add_subsystem( domains_tree, subsystem_name="default-subsystem", subsystem_id=0 ):

    subsystems_node = LopperNode( abspath=f"/domains/{subsystem_name}", name=subsystem_name )
    subsystems_node["compatible"] = "xilinx,subsystem"
    subsystems_node["id"] = subsystem_id
    domains_tree = domains_tree + subsystems_node

    return domains_tree

def isospec_domain( tgt_node, sdt, options ):
    """assist entry point, called from lopper when a node is
       identified, or passed as a command line assist
    """
    try:
        verbose = options['verbose']
    except:
        verbose = 0

    if verbose:
        logging.getLogger().setLevel( level=logging.INFO )
    if verbose > 1:
        logging.getLogger().setLevel( level=logging.DEBUG )

    _info( f"cb: isospec_domain( {tgt_node}, {sdt}, {verbose} )" )

    if sdt.support_files:
        isospec = sdt.support_files.pop()
    else:
        try:
            args = options['args']
            if not args:
                _error( "isospec: no isolation specification passed" )
            isospec = args.pop(0)
        except Exception as e:
            _error( f"isospec: no isolation specification passed: {e}" )
            sys.exit(1)

    domain_yaml_file = "domains.yaml"
    try:
        args = options['args']
        domain_yaml_file = args.pop(0)
    except:
        pass

    try:
        iso_file = Path( isospec )
        iso_file_abs = iso_file.resolve( True )
    except FileNotFoundError as e:
        _error( f"ispec file {isospec} not found" )

    # convert the spec to a LopperTree for consistent manipulation
    json_in = LopperJSON( json=iso_file_abs )
    json_tree = json_in.to_tree()

    # TODO: make the tree manipulations and searching a library function
    domains_tree = domains_tree_start()
    iso_subsystems = json_tree["/design/subsystems"]

    for n in iso_subsystems.children():
        _info( f"infospec_domain: processing subsystem: {n}" )

        isospec_domain_node = json_tree[f"/design/subsystems/{n.name}"]
        domain_id = isospec_domain_node["id"]

        domains_tree = domains_tree_add_subsystem( domains_tree, n.name, domain_id )
        subsystem_node = domains_tree[f"/domains/{n.name}"]

        # cpus
        try:
            iso_cpus = isospec_domain_node["cpus"]
            cpus_list = isospec_process_cpus( iso_cpus, sdt, json_tree )
            subsystem_node["cpus"] = json.dumps(cpus_list)
            subsystem_node.pclass = "json"
        except KeyError as e:
            _info( f"no cpus in /design/subsystems/{n.name}" )
        except Exception as e:
            _error( f"problem during subsystem processing: {e}" )

        # access and memory
        try:
            iso_access = json_tree[f"/design/subsystems/{n.name}/access"]
            access_list,memory_list,sram_list = isospec_process_access( iso_access, sdt, json_tree )
            if memory_list:
                _info( f"memory: {memory_list}" )
                subsystem_node["memory"] = json.dumps(memory_list)
            if sram_list:
                _info( "sram: {memory_list}" )
                subsystem_node["sram"] = json.dumps(sram_list)
            subsystem_node["access"] = json.dumps(access_list)
        except KeyError as e:
            _error( f"no access list in /design/subsystems/{n.name}" )
        except Exception as e:
            _error( f"problem during subsystem processing: {e}" )

    # write the yaml tree
    _info( f"writing domain file: {domain_yaml_file}" )
    sdt.write( domains_tree, output_filename=domain_yaml_file, overwrite=True )

    return True
