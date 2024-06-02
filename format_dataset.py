import argparse
import json
import logging
import os
import subprocess
import sys

import xml.etree.ElementTree as ET
from pprint import pprint
from tqdm import tqdm

sys.path.append("./comment_update")
from data_processing.data_formatting_utils import subtokenize_code, subtokenize_comment
from data_utils import DiffASTExample, DiffTreeNode, DiffAST


class Indexer:
    def __init__(self):
        self.count = 0

    def generate(self):
        new_id = self.count
        self.count += 1
        return new_id


class XMLNode:
    def __init__(
        self,
        value,
        node_id,
        parent,
        attribute,
        alignment_id,
        location_id,
        src,
        is_leaf=True,
    ):
        self.value = value
        self.node_id = node_id
        self.parent = parent
        self.attribute = attribute
        self.alignment_id = alignment_id
        self.location_id = location_id
        self.src = src
        self.is_leaf = is_leaf
        self.children = []
        self.pseudo_children = []
        self.prev_sibling = None
        self.next_sibling = None

    def print_node(self):
        parent_value = None
        if self.parent:
            parent_value = self.parent.value

        print(
            "{}: {} ({}, {})".format(
                self.node_id, self.value, parent_value, len(self.children)
            )
        )
        for c in self.children:
            c.print_node()


class AST:
    def __init__(self, ast_root):
        self.root = ast_root
        self.nodes = []
        self.traverse(ast_root)

    def traverse(self, curr_node):
        self.nodes.append(curr_node)
        for c, child_node in enumerate(curr_node.children):
            if c > 0:
                child_node.prev_sibling = curr_node.children[c - 1]
            if c < len(curr_node.children) - 1:
                child_node.next_sibling = curr_node.children[c + 1]
            self.traverse(child_node)

    @property
    def leaves(self):
        return [n for n in self.nodes if n.is_leaf]


def parse_xml_obj(xml_obj, indexer, parent, src):
    fields = xml_obj.attrib
    attribute = fields["typeLabel"]
    is_leaf = False

    if "label" in fields:
        is_leaf = True
        value = fields["label"]
    else:
        value = attribute

    alignment_id = None
    location_id = "{}-{}-{}-{}".format(
        fields["type"], value, fields["pos"], fields["length"]
    )

    if "other_pos" in fields:
        if src == "old":
            alignment_id = "{}-{}-{}-{}".format(
                fields["pos"],
                fields["length"],
                fields["other_pos"],
                fields["other_length"],
            )
        else:
            alignment_id = "{}-{}-{}-{}".format(
                fields["other_pos"],
                fields["other_length"],
                fields["pos"],
                fields["length"],
            )

    node = XMLNode(
        value,
        indexer.generate(),
        parent,
        attribute,
        alignment_id,
        location_id,
        src,
        is_leaf,
    )

    for child_obj in xml_obj:
        node.children.append(parse_xml_obj(child_obj, indexer, node, src))
    return node


def set_id(diff_node, indexer):
    diff_node.node_id = indexer.generate()
    for node in diff_node.children:
        set_id(node, indexer)


def print_diff_node(diff_node):
    print(
        "{} ({}-{}): {}, {}".format(
            diff_node.value,
            diff_node.src,
            diff_node.node_id,
            [c.value for c in diff_node.children],
            [p.node_id for p in diff_node.parents],
        )
    )
    for child in diff_node.children:
        print_diff_node(child)


def get_ast(new_sample_path, actions_json, jar_path):
    new_xml_path = os.path.join(XML_DIR, "new.xml")

    output = subprocess.check_output(
        [
            "java",
            "-jar",
            jar_path,
            new_sample_path,
            new_xml_path,
            actions_json,
        ]
    )

    xml_obj = ET.parse(new_xml_path)
    new_root = parse_xml_obj(xml_obj.getroot()[1], Indexer(), None, "new")
    new_ast = AST(new_root)

    new_nodes = new_ast.nodes
    new_diff_nodes = [
        DiffTreeNode(n.value, n.attribute, n.src, n.is_leaf) for n in new_nodes
    ]

    for n, new_node in enumerate(new_nodes):
        new_diff_node = new_diff_nodes[n]
        if new_node.parent:
            new_diff_node.parents.append(new_diff_nodes[new_node.parent.node_id])

        for c in new_node.children:
            new_diff_node.children.append(new_diff_nodes[c.node_id])

        if new_node.prev_sibling:
            new_diff_node.prev_siblings.append(
                new_diff_nodes[new_node.prev_sibling.node_id]
            )

        if new_node.next_sibling:
            new_diff_node.next_siblings.append(
                new_diff_nodes[new_node.next_sibling.node_id]
            )

    ast = DiffAST(new_diff_nodes[0])

    return ast


def java_to_ast(java_path, json_dir, lines=False):
    with open(java_path, "r") as f:
        files = json.load(f)
    if lines and "jsonl" not in json_dir:
        json_dir = json_dir.replace("json", "jsonl")

    # files = ex

    for file in tqdm(files):
        tmp_java = "/tmp/tmp.java"
        with open(tmp_java, "w") as f:
            f.write(file["code"])
            ast = get_ast(tmp_java, "old_new_ast_actions.json", JAR_PATH)
        file["ast"] = ast.to_json()
        if lines:
            with open(json_dir, "a") as f:
                f.write(json.dumps(file) + "\n")
    if not lines:
        with open(json_dir, "w") as f:
            json.dump(files, f)


def ast_to_diff_example(ast_dir, de_dir):

    with open(de_dir, "w") as f:
        f.write("")

    with open(ast_dir, "r") as f:
        for i, line in tqdm(enumerate(f)):
            file = json.loads(line)
            de = build_posthoc_test_example(
                id=file["bug_name"],
                label=1,
                comment_type="Summary",
                comment_raw=file["auto_doc"],
                code_raw=file["code"],
                ast=file["ast"],
            )
            with open(de_dir, "a") as de_f:
                de_f.write(json.dumps(de._asdict()) + "\n")


def build_posthoc_test_example(id, label, comment_type, comment_raw, code_raw, ast):
    comment_subtokens = subtokenize_comment(comment_raw).split()
    code_subtokens = subtokenize_code(code_raw).split()
    return DiffASTExample(
        id=id,
        label=label,
        comment_type=comment_type,
        old_comment_raw=comment_raw,
        old_comment_subtokens=comment_subtokens,
        new_comment_raw=None,
        new_comment_subtokens=None,
        span_minimal_diff_comment_subtokens=None,
        old_code_raw=None,
        old_code_subtokens=None,
        new_code_raw=code_raw,
        new_code_subtokens=code_subtokens,
        span_diff_code_subtokens=None,
        token_diff_code_subtokens=None,
        old_ast=None,
        new_ast=ast,
        diff_ast=None,
    )


JAR_PATH = (
    "resources/ast-diffing-1.6-jar-with-dependencies.jar"  # TODO: PATH TO JAR FILE
)
VIEW_DATA_PATH = "UPDATE THIS TO POINT TO/view_data_gpt3.json"  # TODO: PATH TO DATASET
AST_PATH = "out/test.jsonl"
DIFF_EX_PATH = "out/diff_examples.jsonl"
XML_DIR = "xml_files/"

if __name__ == "__main__":
    os.makedirs(XML_DIR, exist_ok=True)

    java_to_ast(VIEW_DATA_PATH, AST_PATH, True)

    ast_to_diff_example(AST_PATH, DIFF_EX_PATH)
