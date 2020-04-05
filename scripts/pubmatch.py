# Python library imports

from pathlib import Path  # construct file paths
from Bio import Entrez    # query the NCBI API
import xml.etree.ElementTree as ET
import pickle
from crossref.restful import Works, Etiquette  # query the Crossref REST API
import nltk
from nltk.tokenize import sent_tokenize, word_tokenize 
import numpy as np
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Any
import tempfile
import os


def get_clean_xml(search_term, pubmed_user, pubmed_key, batch_size, file_cleaned):
    """
    Requirements:
    - requires a search term
    - PubMed user (your email)
    - PubMed API key
    - a batch size that is downloaded from Entrez
    - a file path to write out the data
    Actions:
    - calls the Entrez API
    - prints the number of records for the search term
    - saves webenv and querykey for subsequent searches
    - posts the record IDs to the Entrez history server
    - retrieves result in batches using the history server
    - handles server timeouts and retries http calls
    - deposits search_term at the end of the file
    Output:
    - prints progress along the way
    - deposits batched file to a temp folder
    - cleans repetitive XML headers (result of batching)
    - deposits cleaned file according to file_cleaned path object
    """
    # create temporary file for XML batches
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        file_batched = tf.name
    
    Entrez.email = pubmed_user
    apikey = pubmed_key

    # test the PubMed waters, get the record count and save the history
    handle = Entrez.esearch(db = "pubmed", term = search_term, retmax = 50000, usehistory = "y")
    record = Entrez.read(handle)
    handle.close()
    count = int(record["Count"])

    webenv = record["WebEnv"]
    query_key = record["QueryKey"]

    # first identify the number of counts,
    handle = Entrez.esearch(db = "pubmed", term = search_term, retmax = count)
    record = Entrez.read(handle)

    id_list = record["IdList"]
    assert count == len(id_list)
    print("There are {} records for {}".format(count, search_term))

    post_xml = Entrez.epost("pubmed", id = ",".join(id_list))
    search_results = Entrez.read(post_xml)

    webenv = search_results["WebEnv"]
    query_key = search_results["QueryKey"]

    # generate file handle for the path object
    with open(file_batched, "w", encoding ="utf-8") as out_handle:
        for start in range(0, count, batch_size):
            end = min(count, start + batch_size)
            print("Going to download record %i to %i" % (start+1, end))
            attempt = 0
            while attempt < 3:
                attempt += 1
                try:
                    fetch_handle = Entrez.efetch(db = "pubmed", retmode = "xml",
                                                     retstart = start, retmax = batch_size,
                                                     webenv = webenv, query_key = query_key,
                                                     api_key = apikey)
                except HTTPError as err:
                    if 500 <= err.code <= 599:
                        print("Received error from server %s" % err)
                        print("Attempt %i of 3" % attempt)
                        time.sleep(15)
                    else:
                        raise
            data = fetch_handle.read()
            fetch_handle.close()
            out_handle.write(data)

    # deposit search term as comment at the end of the file
    search_term_comment = "".join(['\n<!--Generated by PubMed search term: ', search_term, "-->\n"])

    with open(file_batched, "a", encoding ="utf-8") as myfile:
        myfile.write(search_term_comment)

    # remove XML header lines that are artifacts of batch process
    problems = ('<?xml version', "<!DOCTYPE PubmedArticleSet PUBLIC", "<PubmedArticleSet", "</PubmedArticleSet")
    with open(file_batched, "r", encoding ="utf-8") as f:
        with file_cleaned.open("w", encoding ="utf-8") as out_file:
            for i in range(10):
                out_file.write(f.readline())
            for line in f:
                if not line.startswith(problems):
                    out_file.write(line)
            out_file.write("</PubmedArticleSet>\n")
    # delete the batched file when the clean file is deposited
    os.unlink(file_batched)

# Create dataclass for articles

@dataclass
# @dataclass_json
class Article:
    my_id: str = field(default = None)
    doi: str = field(default = None)
    pmid: str = field(default = None) # using a field allows to initiate without that info
    authors: List[Any] = field(default_factory = list)
    title: str = field(default = None)
    abstract: str = field(default = None)
    content: str = field(default = None)
    journal: str = field(default = None)
    year: int = field(default = 0)
    references: List[Any] = field(default_factory = list)



# retrieve individual article records from PubMed XML    
def article_from_pubmed(root):
    # root is an ElementTree element with the PubmedArticle tag
    fields = {}
    articleids = root.findall('.//ArticleId')
    for Id in articleids:
        # TODO isn't there a nicer way to do this?
        if 'doi' in Id.attrib.values():
            fields['doi'] = Id.text
        if 'pubmed' in Id.attrib.values():
            fields['pmid'] = Id.text
    if 'doi' in fields:
        fields['my_id'] = fields['doi']
    elif 'pmid' in fields:
        # Only use pmid for my_id if no doi
        fields['my_id'] = fields['pmid']
    authors = []
    for surname in root.findall(".//AuthorList/Author/LastName"):
        # TODO parse full name if needed
        authors.append(surname.text)
    fields['authors'] = authors
    fields['title'] = root.findtext('.//ArticleTitle')
    fields['journal'] = root.findtext('.//ISOAbbreviation')
    fields['year'] =  root.findtext('.//JournalIssue/PubDate/Year')
    abstract = root.find('.//Abstract')
    if abstract:
        fields['abstract'] = ET.tostring(abstract, encoding='utf-8', method='text').decode("utf-8")
    #if (doi AND title = title, authors=authors, journal=journal, year=year)
    return Article(**fields)

def create_corpus(file_cleaned):
    # read XML file and find root
    with file_cleaned.open("r", encoding ="utf-8") as infile:
        tree = ET.parse(infile)
        root = tree.getroot()
    corpus_articles = []
    for article in root.findall('.//PubmedArticle'):
        parsed = article_from_pubmed(article)
        corpus_articles.append(parsed)
    return corpus_articles

def get_pubmed_summary(webenv, query_key, apikey, numrec):
    handle = Entrez.esummary(db="pubmed", retmax = numrec, retmode="xml", webenv = webenv, query_key = query_key, api_key = apikey)
    records = Entrez.parse(handle)
    # build a dict of dicts
    data = {}
    record_id = 0
    for record in records:
        # each record is a Python dictionary or list.
        data[record_id] = data.get(record_id, {})
        data[record_id].update(record)
        record_id += 1       
        print(record['Title']) #, record["AuthorList"]
    handle.close()

