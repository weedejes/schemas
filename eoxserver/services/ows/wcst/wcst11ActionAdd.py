#-----------------------------------------------------------------------
# $Id$
#
# Description: 
#
#   WCS 1.1.x Transaction extension -  implementation of the Add action  
#
#-------------------------------------------------------------------------------
#
# Project: EOxServer <http://eoxserver.org>
# Authors: Martin Paces <martin.paces@iguassu.cz>
#
#-------------------------------------------------------------------------------
# Copyright (C) 2011 Iguassu Software Systems, a.s 
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell 
# copies of the Software, and to permit persons to whom the Software is 
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies of this Software or works derived from this Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#-------------------------------------------------------------------------------

import os 
import sys 
import os.path
import traceback

import logging
import shutil

from eopParse import eop20GetID, eop20SetID
from wcstUtils import wcst11DownloadReference
from wcstUtils import getNewCoverageID
from wcstUtils import timeStampUTC
from wcstUtils import responseUpload, saveToFile
from wcst11Exception import createXML_OWS11Exception

from gdalGeoTiff import GDalInfo, getFootprint

from wcst11ActionCommon import reserveCoverageId, releaseCoverageId, releaseCoverageIds
from wcst11ActionCommon import ExActionFailed
from wcst11ActionCommon import LocalPath
from wcst11ActionCommon import createXML_EOP20

#-------------------------------------------------------------------------------

import eoxserver 

from eoxserver.resources.coverages.covmgrs import CoverageIdManager
from eoxserver.resources.coverages.covmgrs import RectifiedDatasetManager
from eoxserver.resources.coverages.covmgrs import ReferenceableDatasetManager


#-------------------------------------------------------------------------------

# ACTION: ADD 

def wcst11ActionAdd( action , context , maxAttempts = 3 ) : 
    """ WCS-T 1.1 Transaction - Add action handler """ 

    logging.debug( "wcst11ActionAdd()" ) 
    logging.debug( str(action) ) 
    logging.debug( str(context) ) 

    # create coverage Id manager 
    covIdManager = CoverageIdManager()

    # generate internal coverage Id (CID) 
    for i in xrange( maxAttempts ) :
        cid = getNewCoverageID()
        # try to reserve the ID 
        if reserveCoverageId( covIdManager , cid , context['requestId'] ) : break  
    else : 
        msg = "WCSt11:Add: Failed to generate an unique coverage ID!" 
        logging.error( msg ) 
        raise Exception , msg 

    # extract user provided CID 
    coverageId = action["Identifier"] 

    # if the user provided coverage id cannot be used -> fall-back to the internal CID
    if ( coverageId is None ) or ( not reserveCoverageId( covIdManager , coverageId , context['requestId'] ) ):
        coverageId = cid 

    try : 

        # ------------------------------------------------------------------------------

        pixelData    = [] 
        covDescript  = [] 
        geoTransform = []  
        eopXML       = None 

        # download references 
        for i,r in enumerate(action["Reference"]): 

            logging.debug( "WCSt11: Reference: role: %s \t href: %s " % ( r['role'] , r['href'] ) ) 

            basename = os.path.join( context['pathTemp'] , "%s_ref_%3.3i" % ( cid , i ) ) 

            if r['role'] == "urn:ogc:def:role:WCS:1.1:Pixels" : 
                pixelData.append( wcst11DownloadReference( r['href'] , basename ) ) 
            elif r['role'] == "urn:ogc:def:role:WCS:1.1:CoverageDescription" : 
                covDescript.append( wcst11DownloadReference( r['href'] , basename ) )
            elif r['role'] == "urn:ogc:def:role:WCS:1.1:GeoreferencingTransformation" : 
                geoTransform.append( wcst11DownloadReference( r['href'] , basename ) )

        # -MP- NOTE: 
        # I REALLY do not know what to do with the CoverageDescription and GeoreferencingTransformation.
        # They are required by the WCS-T standard (mandatory references which are always supposed to be present)
        # but not by the EOxServer API at all. Therefore I decided to silently ignore their absence. 
        # When provided it is fine, but not I do not need them at all.   

        # ------------------------------------------------------------------------------
        # download metadata 
        for i,r in enumerate(action["Metadata"]): 

            logging.debug( "WCSt11: Metadata: role: %s \t href: %s " % ( r['role'] , r['href'] ) ) 

            basename = os.path.join( context['pathTemp'] , "%s_md_%3.3i" % ( cid , i ) ) 

            if r['role'] == "http://www.opengis.net/eop/2.0/EarthObservation" : 
                eopXML = wcst11DownloadReference( r['href'] , basename ) 

        # ------------------------------------------------------------------------------
        # get information about the pixel data 

        srcTIFfile = pixelData[0][0]

        try: 
            info = GDalInfo( srcTIFfile ) 
        except Exception as e : 
            raise ExActionFailed , "Failed to check the input pixel data! Reason: %s" % str(e) 

        # check that the input data is geotiff 
        if info.driverName != "GeoTIFF" : 
            raise ExActionFailed , "Input pixel data not in required GeoTIFF format! Format: %s " % info.driverName

        # check geocoding 
        if bool( info.isReferenceable ) == bool( info.isRectified ) :   
            raise ExActionFailed , "Input GeoTIFF pixel data not properly geocoded!" 
       
        logging.debug( "WCSt11: Coverage Type: %s " % ("REFERENCEABLE","RECTIFIED")[info.isRectified] ) 

        # ------------------------------------------------------------------------------
        # eopXML check 

        srcXMLfile = os.path.join( context['pathTemp'] , "%s_md.xml" % cid ) 

        isEODataset = False 

        if eopXML is not None :

            logging.debug( "WCSt11: Using the existing EOP2.0 XML file: %s  " % eopXML[0] )

            # The following commands extract the EOP profile from the provided XML document 
            # and changes the EOP ID  to coverageId.
            # The input can be either EOP2.0 XML document or an arbirary XML having EOP2.0 XML as 
            # sub-element. In the latter case, the first instance of EOP2.0 XML element is extracted. 

            f0 = file(eopXML[0]) ; f1 = file( srcXMLfile , "w" ) 

            try : 
                eop20SetID( f0 , f1 , coverageId ) 
            except Exception as e : 
                f0.close() ; f1.close() 
                raise ExActionFailed , "Cannot parse the input EOP2.0 XML Document! Reason: %s" % str(e) 

            f0.close() ; f1.close() 
        
            isEODataset = True 

        # ------------------------------------------------------------------------------
        # in case of missing EO profile extract footprint 

        if not isEODataset : 

            # prepare coverage insert 
            md_start     = timeStampUTC()
            md_stop      = md_start

            md_footprint = getFootprint( info , repeatFirst = True )

            logging.debug( str(info) ) 
            logging.debug( md_footprint ) 

            logging.debug( "WCSt11: EOP2.0 XML not provided! Trying to extract information from the GeoTIFF image." ) 
            logging.debug( "WCSt11: Generating EOP2.0 XML file: %s" % srcXMLfile )

            fid = file( srcXMLfile , "w" ) 
            fid.write( createXML_EOP20( coverageId , md_footprint , md_start , md_stop ) ) 
            fid.close() 

            isEODataset = True 

        # ------------------------------------------------------------------------------
            
        dstXMLfile =  os.path.join( context['pathPerm'] , "%s.xml" % cid )
        dstTIFfile =  os.path.join( context['pathPerm'] , "%s.tif" % cid )

        # move the pixel data to the final destination 
        shutil.move( srcTIFfile , dstTIFfile )                          
        shutil.move( srcXMLfile , dstXMLfile )  # ????                

        # ------------------------------------------------------------------------------
        # rectified dataset 
        if info.isRectified : 

            logging.debug( "WCSt11: Inserting instance of RectifiedDataset ..." ) 

            rdm = RectifiedDatasetManager() 
        
            rdm.create( coverageId , context['requestId'] , 
                local_path = os.path.abspath( dstTIFfile ) , 
                range_type_name = "Grayscale" )  # TODO: proper Range Type selection
            
            
        # referencable dataset 
        elif info.isReferenceable : 

            logging.debug( "WCSt11: Inserting instance of ReferenceableDataset ..." ) 

            rdm = ReferenceableDatasetManager() 

            rdm.create( coverageId , context['requestId'] , 
                local_path = os.path.abspath( dstTIFfile ) , 
                range_type_name = "Grayscale" )  # TODO: proper Range Type selection

        # ------------------------------------------------------------------------------

    except : 
        releaseCoverageIds( covIdManager , ( cid , coverageId ) ) 
        raise 

    # release reserved coverage ids 
    releaseCoverageIds( covIdManager , ( cid , coverageId ) ) 

    return coverageId 
